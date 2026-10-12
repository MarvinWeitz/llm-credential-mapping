import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from llm_credential_mapping.adapters import Credential
from llm_credential_mapping.cli import main
from llm_credential_mapping.customers import Tag
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.repositories import Checkout, RepositoryStore


class CustomerCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = ProfileStore(self.home)
        self.profile = Profile("consulting", "https://gateway.example", {"team_id": "business-unit"})
        self.store.add(self.profile)
        self.repository = "github.com/company/repo"
        self.work = self.home / "work"
        self.work.mkdir()
        self.checkout = Checkout(self.work, self.repository, None)
        # Run in a folder whose .llm-customer search ends at the temporary home directory.
        for patcher in (
            patch("llm_credential_mapping.cli.Path.cwd", return_value=self.work),
            patch("llm_credential_mapping.customers.Path.home", return_value=self.home),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def call(self, *args):
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return main(["--home", str(self.home), *args])

    def test_profile_commands_preserve_the_profile_and_its_identity(self):
        with patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("profile", "list"), 0)
            self.assertEqual(self.call("profile", "use", "consulting"), 0)
        self.assertEqual(self.store.get(), self.profile)
        self.assertEqual(self.store.get().account, self.profile.account)

    def test_picker_filters_the_catalogue_by_prefix(self):
        stdin = io.StringIO("1\n")
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as manager, \
                patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                patch.object(stdin, "isatty", return_value=True), \
                patch("llm_credential_mapping.gateways.litellm.list_tags", return_value=[Tag("other"), Tag("customer:B")]), \
                patch("llm_credential_mapping.cli.launch", return_value=0) as launch:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(self.call("run", "--tag-prefix", "customer:", "claude"), 0)
        self.assertEqual(launch.call_args.args[2].customer, "customer:B")

    def test_explicit_customer_needs_neither_git_nor_catalogue_and_is_not_saved(self):
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as manager, \
                patch("llm_credential_mapping.cli.find_checkout", return_value=None), \
                patch("llm_credential_mapping.gateways.litellm.list_tags", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.launch", return_value=7) as launch:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(self.call("run", "--profile", "consulting", "--customer", "customer:B", "claude"), 7)
        self.assertEqual(launch.call_args.args[2].customer, "customer:B")
        self.assertFalse((self.home / "repositories.json").exists())
        self.assertIn("pinned", self.stderr.getvalue())
        self.assertNotIn("secret", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_bound_customer_skips_picker_and_catalogue(self):
        RepositoryStore(self.store).bind(self.profile.gateway, self.repository, "customer:A")
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as manager, \
                patch("llm_credential_mapping.gateways.litellm.list_tags", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.launch", return_value=0) as launch:
            manager.return_value.credential.return_value = Credential("same-secret")
            self.assertEqual(self.call("run", "claude"), 0)
        self.assertEqual(launch.call_args.args[2].customer, "customer:A")

    def test_unknown_repo_interactive_selection_is_persisted_once(self):
        stdin = io.StringIO("2\n")
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as manager, \
                patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                patch.object(stdin, "isatty", return_value=True), \
                patch("llm_credential_mapping.gateways.litellm.list_tags", return_value=[Tag("customer:A"), Tag("customer:B")]) as catalogue, \
                patch("llm_credential_mapping.cli.launch", return_value=0) as launch:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(self.call("run", "--tag-prefix", "customer:", "claude"), 0)
        self.assertEqual(launch.call_args.args[2].customer, "customer:B")
        self.assertEqual(RepositoryStore(self.store).get(self.profile.gateway, self.repository), "customer:B")
        catalogue.assert_called_once_with(self.profile, "secret")

    def test_noninteractive_unbound_repo_never_launches_or_reads_secrets(self):
        stdin = io.StringIO()
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout), \
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
                    patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout), \
                    patch("llm_credential_mapping.cli.NativeKeyring"), \
                    patch("llm_credential_mapping.cli.auth_for"), \
                    patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                    patch.object(stdin, "isatty", return_value=True), \
                    patch("llm_credential_mapping.gateways.litellm.list_tags", return_value=catalogue), \
                    patch("llm_credential_mapping.cli.launch", side_effect=AssertionError):
                self.assertEqual(self.call("run", "claude"), 1)
            self.assertFalse((self.home / "repositories.json").exists())

    def test_empty_gateway_catalogue_points_to_central_rules(self):
        stdin = io.StringIO("1\n")
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for"), \
                patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                patch.object(stdin, "isatty", return_value=True), \
                patch("llm_credential_mapping.gateways.litellm.list_tags", return_value=[]), \
                patch("llm_credential_mapping.cli.launch", side_effect=AssertionError):
            self.assertEqual(self.call("run", "claude"), 1)
        self.assertIn("central rules", self.stderr.getvalue())
        self.assertIn("--customer", self.stderr.getvalue())

    def test_picker_catalogue_failure_is_reported_not_guessed(self):
        stdin = io.StringIO("1\n")
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for"), \
                patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                patch.object(stdin, "isatty", return_value=True), \
                patch("llm_credential_mapping.gateways.litellm.list_tags", side_effect=CredentialError("Tag catalogue HTTP 403")), \
                patch("llm_credential_mapping.cli.launch", side_effect=AssertionError):
            self.assertEqual(self.call("run", "claude"), 1)
        self.assertIn("403", self.stderr.getvalue())
        self.assertFalse((self.home / "repositories.json").exists())

    def test_binding_and_unbinding_explicit_customer_need_no_keyring(self):
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout), \
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
                patch("llm_credential_mapping.cli.auth_for"), \
                patch("llm_credential_mapping.cli.launch", return_value=0):
            self.assertEqual(self.call("run", "--customer", "customer:B", "claude"), 0)
        self.assertEqual(repositories.get(self.profile.gateway, self.repository), "customer:A")

    def test_tag_list_json_has_only_nonsecret_catalogue_fields(self):
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as manager, \
                patch("llm_credential_mapping.gateways.litellm.list_tags", return_value=[Tag("customer:A", "Customer A")]) as catalogue:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(self.call("tags", "list", "--profile", "consulting", "--prefix", "customer:", "--json"), 0)
        self.assertEqual(json.loads(self.stdout.getvalue()), [{"name": "customer:A", "description": "Customer A"}])
        catalogue.assert_called_once_with(self.profile, "secret")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_terminal_control_characters_in_tag_catalogue_are_escaped(self):
        with patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for"), \
                patch("llm_credential_mapping.gateways.litellm.list_tags", return_value=[Tag("customer:A", "\x1b[31msecret-looking\ntext")]):
            self.assertEqual(self.call("tags", "list"), 0)
        self.assertNotIn("\x1b", self.stdout.getvalue())
        self.assertIn("\\u001b", self.stdout.getvalue())

    def show(self, checkout=None):
        self.stdout.truncate(0)
        self.stdout.seek(0)
        with patch("llm_credential_mapping.cli.find_checkout", return_value=checkout), \
                patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError):
            result = self.call("repo", "show", "--json")
        return result, json.loads(self.stdout.getvalue()) if result == 0 else None

    def test_repo_show_reports_unmapped_folders_without_secrets(self):
        self.assertEqual(self.show(self.checkout), (1, None))
        self.assertIn(self.repository, self.stderr.getvalue())
        self.assertIn("repo bind --shared", self.stderr.getvalue())

    def test_shared_file_rules_and_personal_override_resolve_in_order(self):
        rules = self.home / "rules.json"
        rules.write_text(json.dumps({
            "version": 1,
            "customers": [{"id": "customer:rules", "name": "From rules"}],
            "rules": [{"match": "github.com/company/**", "customer": "customer:rules"}],
        }), encoding="utf-8")
        self.assertEqual(self.call("profile", "update", "consulting", "--rules", str(rules)), 0)
        result, shown = self.show(self.checkout)
        self.assertEqual((result, shown["customer"], shown["source"]), (0, "customer:rules", "rules"))
        self.assertEqual(shown["headers"]["x-litellm-tags"], "customer:rules")
        self.assertEqual(
            json.loads(shown["headers"]["x-litellm-spend-logs-metadata"]),
            {"customer_source": "rules", "repository": self.repository},
        )
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout):
            self.assertEqual(self.call("repo", "bind", "--shared", "--customer", "customer:file"), 0)
        self.assertEqual(
            (self.work / ".llm-customer").read_text(encoding="utf-8").splitlines()[-1], 'customer = "customer:file"',
        )
        self.assertEqual(self.show(self.checkout)[1]["source"], "file")
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout):
            self.assertEqual(self.call("repo", "bind", "--customer", "customer:mine"), 0)
        self.assertEqual(self.show(self.checkout)[1]["customer"], "customer:mine")
        with patch.dict("os.environ", {"LLM_CREDENTIALS_CUSTOMER": "customer:env"}):
            self.assertEqual(self.show(self.checkout)[1]["customer"], "customer:env")

    def test_shared_file_assigns_folders_outside_git(self):
        with patch("llm_credential_mapping.cli.find_checkout", return_value=None), \
                patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("repo", "bind", "--customer", "customer:A"), 1)
            self.assertIn("--shared", self.stderr.getvalue())
            self.assertEqual(self.call("repo", "bind", "--shared", "--customer", "customer:A"), 0)
        result, shown = self.show()
        self.assertEqual((shown["customer"], shown["repository"]), ("customer:A", None))
        self.assertEqual(shown["headers"]["x-litellm-spend-logs-metadata"], '{"customer_source":"file"}')

    def test_picker_prefers_the_rules_catalogue_and_saves_a_personal_override(self):
        rules = self.home / "rules.json"
        rules.write_text(json.dumps({"version": 1, "customers": [{"id": "customer:central"}]}), encoding="utf-8")
        self.call("profile", "update", "consulting", "--rules", str(rules))
        stdin = io.StringIO("1\n")
        with patch("llm_credential_mapping.cli.find_checkout", return_value=self.checkout), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as manager, \
                patch("llm_credential_mapping.cli.sys.stdin", stdin), \
                patch.object(stdin, "isatty", return_value=True), \
                patch("llm_credential_mapping.gateways.litellm.list_tags", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.launch", return_value=0) as launch:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(self.call("run", "claude"), 0)
        self.assertEqual(launch.call_args.args[2].customer, "customer:central")
        self.assertEqual(RepositoryStore(self.store).get(self.profile.gateway, self.repository), "customer:central")

    def test_rules_location_can_be_changed_without_touching_the_sign_in(self):
        account = self.store.get().account
        for args in (["--rules", "https://rules.example/customers.json"], ["--no-rules"]):
            self.assertEqual(self.call("profile", "update", "consulting", *args), 0)
            self.assertEqual(self.store.get().account, account)
        self.assertIsNone(self.store.get().rules)
        for location in ("relative/rules.json", "http://rules.example/x.json", "https://user:pw@rules.example/x"):
            with self.subTest(location=location):
                self.assertEqual(self.call("profile", "update", "consulting", "--rules", location), 1)

    def test_invalid_customer_fails_before_keyring_or_launch(self):
        with patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.launch", side_effect=AssertionError):
            self.assertEqual(self.call("run", "--customer", "customer:A,customer:B", "claude"), 1)
