import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.cli import main
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.repositories import RepositoryStore
from llm_credential_mapping.tags import Tag


class CustomerCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = ProfileStore(self.home)
        self.profile = Profile("consulting", "https://gateway.example", "business-unit")
        self.store.add(self.profile)
        self.repository = "github.com/company/repo"
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def call(self, *args):
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return main(["--home", str(self.home), *args])

    def test_profile_alias_preserves_existing_profile_and_vault_identity(self):
        with patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("profile", "list"), 0)
            self.assertEqual(self.call("profile", "use", "consulting"), 0)
        self.assertEqual(self.store.get().account, self.profile.account)

    def test_explicit_customer_needs_neither_git_nor_catalogue_and_is_not_saved(self):
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.SessionManager") as manager, \
                patch("llm_credential_mapping.cli.current_repository", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.list_tags", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.launch", return_value=7) as launch:
            manager.return_value.token.return_value = "secret"
            self.assertEqual(self.call("run", "--profile", "consulting", "--customer", "customer:B", "claude"), 7)
        self.assertEqual(launch.call_args.args[5], "customer:B")
        self.assertFalse((self.home / "repositories.json").exists())
        self.assertIn("pinned", self.stderr.getvalue())
        self.assertNotIn("secret", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_bound_customer_skips_picker_and_catalogue(self):
        RepositoryStore(self.store).bind(self.profile.gateway, self.repository, "customer:A")
        with patch("llm_credential_mapping.cli.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.SessionManager") as manager, \
                patch("llm_credential_mapping.cli.list_tags", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.launch", return_value=0) as launch:
            manager.return_value.token.return_value = "same-secret"
            self.assertEqual(self.call("run", "claude"), 0)
        self.assertEqual(launch.call_args.args[5], "customer:A")

    def test_unknown_repo_interactive_selection_is_persisted_once(self):
        stdin = io.StringIO("2\n")
        with patch("llm_credential_mapping.cli.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.SessionManager") as manager, \
                patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                patch.object(stdin, "isatty", return_value=True), \
                patch("llm_credential_mapping.cli.list_tags", return_value=[Tag("customer:A"), Tag("customer:B")]) as catalogue, \
                patch("llm_credential_mapping.cli.launch", return_value=0) as launch:
            manager.return_value.token.return_value = "secret"
            self.assertEqual(self.call("run", "--tag-prefix", "customer:", "claude"), 0)
        self.assertEqual(launch.call_args.args[5], "customer:B")
        self.assertEqual(RepositoryStore(self.store).get(self.profile.gateway, self.repository), "customer:B")
        catalogue.assert_called_once_with(self.profile, "secret", "customer:")

    def test_noninteractive_unbound_repo_never_launches_or_reads_secrets(self):
        stdin = io.StringIO()
        with patch("llm_credential_mapping.cli.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.launch", side_effect=AssertionError):
            self.assertEqual(self.call("run", "claude"), 1)
        self.assertIn("--customer", self.stderr.getvalue())
        self.assertFalse((self.home / "repositories.json").exists())

    def test_empty_failed_or_invalid_picker_never_binds_or_launches(self):
        for catalogue, answer in (
            ([], "1\n"), ([Tag("customer:A")], "99\n"), ([Tag("customer:A")], ""),
            ([Tag("customer:A")], "9" * 5000 + "\n"),
        ):
            stdin = io.StringIO(answer)
            with self.subTest(catalogue=catalogue, answer=answer), \
                    patch("llm_credential_mapping.cli.current_repository", return_value=self.repository), \
                    patch("llm_credential_mapping.cli.NativeKeyring"), \
                    patch("llm_credential_mapping.cli.SessionManager"), \
                    patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                    patch.object(stdin, "isatty", return_value=True), \
                    patch("llm_credential_mapping.cli.list_tags", return_value=catalogue), \
                    patch("llm_credential_mapping.cli.launch", side_effect=AssertionError):
                self.assertEqual(self.call("run", "claude"), 1)
            self.assertFalse((self.home / "repositories.json").exists())

    def test_picker_catalogue_failure_is_reported_not_guessed(self):
        stdin = io.StringIO("1\n")
        with patch("llm_credential_mapping.cli.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.SessionManager"), \
                patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                patch.object(stdin, "isatty", return_value=True), \
                patch("llm_credential_mapping.cli.list_tags", side_effect=CredentialError("Tag catalogue HTTP 403")), \
                patch("llm_credential_mapping.cli.launch", side_effect=AssertionError):
            self.assertEqual(self.call("run", "claude"), 1)
        self.assertIn("403", self.stderr.getvalue())
        self.assertFalse((self.home / "repositories.json").exists())

    def test_binding_and_unbinding_explicit_customer_need_no_keyring(self):
        with patch("llm_credential_mapping.cli.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("repo", "bind", "--customer", "customer:A"), 0)
            self.assertEqual(self.call("repo", "bind", "--customer", "customer:B"), 0)
            self.assertEqual(self.call("repo", "list"), 0)
            self.assertEqual(RepositoryStore(self.store).get(self.profile.gateway, self.repository), "customer:B")
            self.assertEqual(self.call("repo", "unbind"), 0)
        self.assertIsNone(RepositoryStore(self.store).get(self.profile.gateway, self.repository))

    def test_explicit_override_does_not_modify_repository_default(self):
        repositories = RepositoryStore(self.store)
        repositories.bind(self.profile.gateway, self.repository, "customer:A")
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.SessionManager"), \
                patch("llm_credential_mapping.cli.launch", return_value=0):
            self.assertEqual(self.call("run", "--customer", "customer:B", "claude"), 0)
        self.assertEqual(repositories.get(self.profile.gateway, self.repository), "customer:A")

    def test_tag_list_json_has_only_nonsecret_catalogue_fields(self):
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.SessionManager") as manager, \
                patch("llm_credential_mapping.cli.list_tags", return_value=[Tag("customer:A", "Customer A")]) as catalogue:
            manager.return_value.token.return_value = "secret"
            self.assertEqual(self.call("tags", "list", "--profile", "consulting", "--prefix", "customer:", "--json"), 0)
        self.assertEqual(json.loads(self.stdout.getvalue()), [{"name": "customer:A", "description": "Customer A"}])
        catalogue.assert_called_once_with(self.profile, "secret", "customer:")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_terminal_control_characters_in_tag_catalogue_are_escaped(self):
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.SessionManager"), \
                patch("llm_credential_mapping.cli.list_tags", return_value=[Tag("customer:A", "\x1b[31msecret-looking\ntext")]):
            self.assertEqual(self.call("tags", "list"), 0)
        self.assertNotIn("\x1b", self.stdout.getvalue())
        self.assertIn("\\u001b", self.stdout.getvalue())

    def test_invalid_customer_fails_before_keyring_or_launch(self):
        with patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.launch", side_effect=AssertionError):
            self.assertEqual(self.call("run", "--customer", "customer:A,customer:B", "claude"), 1)
