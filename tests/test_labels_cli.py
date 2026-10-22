import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from virtual_key_mapper.adapters import Credential
from virtual_key_mapper.cli import main
from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.labels import LAUNCH_VARIABLE, MISSING_PHRASE, NO_LABELS_NOTICE, Choice, LabelDefinition, OutputOverride
from virtual_key_mapper.profiles import Profile, ProfileStore
from virtual_key_mapper.repositories import Checkout, RepositoryStore

LABELS = (LabelDefinition("customer", required=True, description="Who pays"), LabelDefinition("project"))
CHOICES = (LabelDefinition(
    "customer", required=True, values=(Choice("acme", "Acme"), Choice("globex")), strict=True,
), LabelDefinition("project"))


class LabelCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.store = ProfileStore(self.home)
        self.profile = Profile("consulting", "https://gateway.example", {"team_id": "business-unit"}, labels=LABELS)
        self.store.add(self.profile)
        self.repository = "github.com/company/repo"
        self.work = self.home / "work"
        self.work.mkdir()
        self.checkout = Checkout(self.work, self.repository, None)
        # Run in a folder whose .vkm search ends at the temporary home directory, without labels from the environment.
        environment = {name: value for name, value in os.environ.items() if not name.startswith(("VKM_LABEL", "VKM_LAUNCH"))}
        for patcher in (
            patch("virtual_key_mapper.cli.Path.cwd", return_value=self.work),
            patch("virtual_key_mapper.resolve.Path.home", return_value=self.home),
            patch.dict(os.environ, environment, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def call(self, *args):
        self.stdout.truncate(0)
        self.stdout.seek(0)
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return main(["--home", str(self.home), *args])

    def use(self, *labels, outputs=None):
        self.store.update("consulting", lambda profile: Profile(
            profile.name, profile.gateway, profile.auth, labels=labels, outputs=outputs or {},
        ))
        self.profile = self.store.get()

    def bound(self):
        return RepositoryStore(self.store).get(self.profile.gateway, self.repository)

    def unbind(self):
        if self.bound():
            RepositoryStore(self.store).unbind(self.profile.gateway, self.repository)

    @contextlib.contextmanager
    def terminal(self, answer, catalogue=None, checkout=True):
        """A terminal where ``answer`` is typed; ``catalogue`` is the gateway's tag list, an error, or None (unused)."""
        stdin = io.StringIO(answer)
        if catalogue is None:
            tags = {"side_effect": AssertionError("the catalogue is not needed")}
        elif isinstance(catalogue, Exception):
            tags = {"side_effect": catalogue}
        else:
            tags = {"return_value": catalogue}
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout if checkout else None), \
                patch("virtual_key_mapper.cli.NativeKeyring"), \
                patch("virtual_key_mapper.cli.auth_for") as manager, \
                patch("virtual_key_mapper.cli.sys.stdin", stdin), \
                patch.object(stdin, "isatty", return_value=True), \
                patch("virtual_key_mapper.gateways.litellm.list_tags", **tags) as listed, \
                patch("virtual_key_mapper.cli.launch", return_value=0) as launch:
            manager.return_value.credential.return_value = Credential("secret")
            yield launch, listed

    def rules_file(self):
        rules = self.home / "rules.json"
        rules.write_text(json.dumps({
            "version": "2.0",
            "labels": [{"key": "customer", "required": True, "values": [{"id": "central", "name": "From rules"}]}],
            "rules": [{"match": "github.com/company/**", "labels": {"customer": "central"}}],
        }), encoding="utf-8")
        return rules

    def show(self, checkout=None, *args):
        with patch("virtual_key_mapper.cli.find_checkout", return_value=checkout), \
                patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            result = self.call("repo", "show", "--json", *args)
        return result, json.loads(self.stdout.getvalue()) if result == 0 else None

    # Profiles

    def test_profile_commands_preserve_the_profile_and_its_identity(self):
        with patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("profile", "list"), 0)
            self.assertEqual(self.call("profile", "use", "consulting"), 0)
        self.assertEqual(self.store.get(), self.profile)
        self.assertEqual(self.store.get().account, self.profile.account)

    def test_profile_labels_and_outputs_are_edited_with_options(self):
        with patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call(
                "profile", "update", "consulting", "--label", "customer", "--required", "--description", "Who pays",
                "--value", "acme=Acme", "--value", "globex", "--strict", "--source", "local", "--source", "explicit",
            ), 0)
            self.assertEqual(self.call(
                "profile", "update", "consulting", "--output", "tags=${customer}", "--output", "tags=${customer}:${project}",
                "--output-off", "metadata",
            ), 0)
            self.assertEqual(self.call("profile", "show", "--json"), 0)
        shown = json.loads(self.stdout.getvalue())
        self.assertEqual(shown["labels"], [
            {"key": "customer", "required": True, "description": "Who pays",
             "values": [{"id": "acme", "name": "Acme"}, {"id": "globex"}], "strict": True, "sources": ["explicit", "local"]},
            {"key": "project"},
        ])
        self.assertEqual(shown["outputs"], {
            "tags": {"format": ["${customer}", "${customer}:${project}"]}, "metadata": {"enabled": False},
        })
        # --output in a later call replaces the whole list; --output-default and --no-label undo.
        self.assertEqual(self.call("profile", "update", "consulting", "--output", "tags=${customer}"), 0)
        self.assertEqual(self.store.get().outputs["tags"], OutputOverride(("${customer}",)))
        self.assertEqual(self.call(
            "profile", "update", "consulting", "--output-default", "tags", "--output-default", "metadata",
            "--no-label", "project",
        ), 0)
        self.assertEqual((self.store.get().outputs, [item.key for item in self.store.get().labels]), ({}, ["customer"]))
        self.assertEqual(self.store.get().account, self.profile.account)

    def test_profile_changes_that_would_misattribute_are_refused_before_saving(self):
        before = (self.home / "profiles.json").read_text(encoding="utf-8")
        for args, message in (
            (["--output", "tags=${nope}"], "nope"),
            (["--output", "tags=${project}"], "reaches no output"),
            (["--output", "tags=${customer}", "--output-off", "tags"], "more than one option"),
            (["--output", "end_user=${customer}"], "no output 'end_user'"),
            (["--output-off", "metadata", "--output", "tags=${customer}${project}"], "separated by literal text"),
            (["--output", "tags"], "NAME=FORMAT"),
            (["--required"], "--label KEY"),
            (["--label", "Customer"], "Invalid label key"),
            (["--label", "team", "--value", "a,b"], "Label value"),
            (["--label", "team", "--strict"], "strict needs values"),
            (["--no-label", "team"], "no label 'team'"),
            (["--definitions", "{"], "--definitions"),
            (["--definitions", '{"labels": [{"key": "customer", "requried": true}]}'], "requried"),
            ([], "Nothing to change"),
            ([], "--output-off, --output-default"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.call("profile", "update", "consulting", *args), 1)
                self.assertIn(message, self.stderr.getvalue())
        self.assertEqual((self.home / "profiles.json").read_text(encoding="utf-8"), before)

    def test_profile_help_describes_every_action(self):
        with self.assertRaises(SystemExit):
            self.call("profile", "--help")
        for action in ("add", "update", "list", "show", "use"):
            with self.subTest(action=action):
                line = next(line for line in self.stdout.getvalue().splitlines() if line.strip().startswith(action + " "))
                self.assertGreater(len(line.split()), 2, line)

    def test_profile_add_takes_the_whole_definitions_in_one_argument(self):
        definitions = {"labels": [{"key": "customer", "required": True}], "outputs": {"tags": {"format": ["${customer}"]}}}
        with patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call(
                "profile", "add", "other", "--gateway", "https://other.example", "--team-id", "t",
                f"--definitions={json.dumps(definitions)}",
            ), 0)
            self.assertEqual(self.call("profile", "show", "other", "--json"), 0)
        shown = json.loads(self.stdout.getvalue())
        self.assertEqual((shown["labels"], shown["outputs"]), (definitions["labels"], definitions["outputs"]))
        self.assertEqual(shown["auth"], {"type": "litellm-sso", "team_id": "t"})

    def test_a_profile_label_for_a_central_key_is_refused_when_the_rules_can_be_read(self):
        rules = self.rules_file()
        self.assertEqual(self.call("profile", "update", "consulting", "--rules", str(rules)), 1)
        self.assertIn("The label rules define 'customer'", self.stderr.getvalue())
        self.assertIsNone(self.store.get().rules)
        self.assertEqual(self.call("profile", "update", "consulting", "--rules", str(rules), "--no-label", "customer"), 0)
        self.assertEqual(self.call("labels", "list"), 0)
        self.assertEqual(self.stdout.getvalue().splitlines(), [
            "customer (required, central rules)", '  values: "central"', "project (optional, profile)",
        ])
        self.assertEqual(self.call("labels", "list", "--json"), 0)
        self.assertEqual([item["origin"] for item in json.loads(self.stdout.getvalue())], ["rules", "profile"])

    def test_a_profile_is_still_checked_on_its_own_when_the_rules_cannot_be_read(self):
        before = (self.home / "profiles.json").read_text(encoding="utf-8")
        rules = ["--rules", "https://rules.example/labels.json"]
        with patch("virtual_key_mapper.cli.RulesLoader", side_effect=CredentialError("offline")), \
                patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            for args, message in (
                (["--output", "tags=${project}"], "'customer' reaches no output"),
                (["--output", "tags=${customer}${team}"], "separated by literal text"),
                (["--output", "tags=${customer}", "--output", "tags=${team}"], "could then read as another"),
                (["--output-off", "tags", "--output-off", "metadata"], "'customer' reaches no output"),
            ):
                with self.subTest(args=args):
                    self.assertEqual(self.call("profile", "update", "consulting", *rules, *args), 1)
                    self.assertIn(message, self.stderr.getvalue())
            self.assertEqual((self.home / "profiles.json").read_text(encoding="utf-8"), before)
            # Placeholders the central document may define are not refused: 'team' could be a central required label.
            self.assertEqual(self.call("profile", "update", "consulting", *rules, "--output", "tags=${customer}:${team}"), 0)
        self.assertEqual(self.store.get().outputs["tags"], OutputOverride(("${customer}:${team}",)))

    def test_rules_location_can_be_changed_without_touching_the_sign_in(self):
        account = self.store.get().account
        with patch("virtual_key_mapper.cli.RulesLoader", side_effect=CredentialError("offline")):
            for args in (["--rules", "https://rules.example/labels.json"], ["--no-rules"]):
                self.assertEqual(self.call("profile", "update", "consulting", *args), 0)
                self.assertEqual(self.store.get().account, account)
        self.assertIsNone(self.store.get().rules)
        self.assertIn("could not be read to check the profile", self.stderr.getvalue())
        for location in ("relative/rules.json", "http://rules.example/x.json", "https://user:pw@rules.example/x"):
            with self.subTest(location=location):
                self.assertEqual(self.call("profile", "update", "consulting", "--rules", location), 1)

    # vkm run

    def test_picker_offers_the_gateway_catalogue_and_saves_a_personal_binding(self):
        with self.terminal("2\n", [Choice("other"), Choice("customer:A"), Choice("customer:B")]) as (launch, listed):
            self.assertEqual(self.call("run", "claude"), 0)
        label = launch.call_args.args[2].get("customer")
        self.assertEqual((label.value, label.source), ("B", "local"))
        self.assertEqual(self.bound(), {"customer": "B"})
        listed.assert_called_once_with(self.profile, "secret")
        self.assertIn("not an authorization list", self.stderr.getvalue())
        self.assertIn('"Who pays"', self.stderr.getvalue())

    def test_picker_offers_the_definitions_values_without_the_catalogue(self):
        self.use(*CHOICES)
        with self.terminal("acme\n") as (launch, _):
            self.assertEqual(self.call("run", "claude"), 0)
        self.assertEqual(launch.call_args.args[2].values, {"customer": "acme"})
        self.assertIn('1. "acme" - "Acme"', self.stderr.getvalue())

    def test_without_catalogue_entries_the_picker_accepts_a_valid_typed_value(self):
        for catalogue in ([], [Choice("unrelated")]):
            self.unbind()
            with self.subTest(catalogue=catalogue), self.terminal("acme\n", catalogue) as (launch, _):
                self.assertEqual(self.call("run", "claude"), 0)
                self.assertEqual(launch.call_args.args[2].values, {"customer": "acme"})
        self.use(LABELS[0], outputs={"tags": OutputOverride(None, False)})  # No list entry: no catalogue at all.
        self.unbind()
        with self.terminal("globex\n"):
            self.assertEqual(self.call("run", "claude"), 0)
        self.assertEqual(self.bound(), {"customer": "globex"})

    def test_explicit_labels_need_neither_git_nor_catalogue_and_are_not_saved(self):
        with self.terminal("", checkout=False) as (launch, _):
            launch.return_value = 7
            self.assertEqual(self.call("run", "--profile", "consulting", "--label", "customer=B", "--label", "project=", "claude"), 7)
        attribution = launch.call_args.args[2]
        self.assertEqual((attribution.values, attribution.get("project").source), ({"customer": "B"}, "explicit"))
        self.assertFalse((self.home / "repositories.json").exists())
        self.assertIn("pinned", self.stderr.getvalue())
        self.assertIn('customer (required) = "B"  [explicit: --label]', self.stderr.getvalue())
        self.assertNotIn("secret", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_bound_labels_skip_the_picker_and_explicit_ones_do_not_change_them(self):
        RepositoryStore(self.store).bind(self.profile.gateway, self.repository, {"customer": "A"})
        with self.terminal("") as (launch, _):
            self.assertEqual(self.call("run", "claude"), 0)
            self.assertEqual(launch.call_args.args[2].values, {"customer": "A"})
            self.assertEqual(self.call("run", "--label", "customer=B", "claude"), 0)
            self.assertEqual(launch.call_args.args[2].values, {"customer": "B"})
        self.assertEqual(self.bound(), {"customer": "A"})

    def test_noninteractive_unbound_repo_never_launches_or_reads_secrets(self):
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout), \
                patch("virtual_key_mapper.cli.sys.stdin", io.StringIO()), \
                patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError), \
                patch("virtual_key_mapper.cli.launch", side_effect=AssertionError):
            self.assertEqual(self.call("run", "claude"), 1)
        self.assertTrue(self.stderr.getvalue().startswith(f"Error: {MISSING_PHRASE} customer for repository {self.repository}"))
        self.assertFalse((self.home / "repositories.json").exists())

    def test_an_explicit_no_value_is_not_asked_for(self):
        with self.terminal("acme\n") as (launch, _):
            self.assertEqual(self.call("run", "--label", "customer=", "claude"), 1)
        launch.assert_not_called()
        self.assertIn(MISSING_PHRASE, self.stderr.getvalue())
        self.assertFalse((self.home / "repositories.json").exists())

    def test_empty_failed_or_invalid_picker_never_binds_or_launches(self):
        for catalogue, answer in (
            ([Choice("customer:A")], "99\n"), ([Choice("customer:A")], ""), ([Choice("customer:A")], "customer:A\n"),
            ([Choice("customer:A")], "9" * 5000 + "\n"), ([], "a,b\n"), ([], "\n"),
            (CredentialError("Tag catalogue HTTP 403"), "1\n"),
        ):
            with self.subTest(catalogue=catalogue, answer=answer), self.terminal(answer, catalogue) as (launch, _):
                self.assertEqual(self.call("run", "claude"), 1)
                launch.assert_not_called()
            self.assertFalse((self.home / "repositories.json").exists())
        self.assertIn("403", self.stderr.getvalue())

    def test_invalid_or_unknown_labels_fail_before_keyring_or_launch(self):
        for args, message in (
            (["--label", "customer=A,B"], "Label value"),
            (["--label", "custmer=A"], "No label 'custmer' is defined; defined labels: customer, project."),
            (["--label", "customer"], "KEY=VALUE"),
            (["--label", "customer=A", "--label", "customer=B"], "more than once"),
        ):
            with self.subTest(args=args), patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError), \
                    patch("virtual_key_mapper.cli.launch", side_effect=AssertionError):
                self.assertEqual(self.call("run", *args, "claude"), 1)
                self.assertIn(message, self.stderr.getvalue())
        self.use(*CHOICES)
        self.assertEqual(self.call("run", "--label", "customer=initech", "claude"), 1)
        self.assertIn("vkm labels values customer", self.stderr.getvalue())

    def test_no_labels_defined_is_said_once_by_text_commands_only(self):
        self.use()
        with self.terminal("") as (launch, _):
            self.assertEqual(self.call("run", "claude"), 0)
        self.assertEqual(self.stderr.getvalue().count(NO_LABELS_NOTICE), 1)
        self.assertEqual(launch.call_args.args[2].labels, ())
        self.stderr = io.StringIO()
        shown = self.show(self.checkout)[1]
        self.assertEqual((shown["labels"], shown["complete"], shown["message"]), ([], True, NO_LABELS_NOTICE))
        self.assertEqual(json.loads(shown["headers"]["x-litellm-spend-logs-metadata"]), {
            "labels": {}, "sources": {}, "repository": self.repository,
        })
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout):
            self.assertEqual(self.call("repo", "show", "--env", "bash"), 0)
            self.assertEqual(self.stderr.getvalue(), "")
            self.assertEqual(self.call("repo", "show"), 0)
        self.assertIn(NO_LABELS_NOTICE, self.stderr.getvalue())

    # vkm repo

    def test_binding_and_unbinding_need_no_keyring(self):
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout), \
                patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("repo", "bind", "--label", "customer=A"), 0)
            self.assertEqual(self.call("repo", "bind", "--label", "customer=B"), 1)
            self.assertIn("--replace", self.stderr.getvalue())
            self.assertEqual(self.call("repo", "bind", "--replace", "--label", "customer=B", "--label", "project=web"), 0)
            self.assertIn('customer="B", project="web"', self.stdout.getvalue())
            self.assertEqual(self.call("repo", "list"), 0)
            self.assertEqual(json.loads(self.stdout.getvalue())[0]["labels"], {"customer": "B", "project": "web"})
            self.assertEqual(self.call("repo", "unbind", "--label", "project"), 0)
            self.assertEqual(self.bound(), {"customer": "B"})
            for args in (["--label", "team=x"], ["--label", "project="], ["--label", "project"]):
                with self.subTest(args=args):
                    self.assertEqual(self.call("repo", "bind", *args), 1)
            self.assertEqual(self.call("repo", "unbind"), 0)
        self.assertEqual(self.bound(), {})

    def test_bind_and_unbind_succeed_when_the_claude_refresh_fails(self):
        (self.work / ".vkm").write_text("[labels]\ncustomer = 'a,b'\n", encoding="utf-8")
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout), \
                patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("repo", "bind", "--label", "customer=acme"), 0)
            self.assertEqual(self.bound(), {"customer": "acme"})
            self.assertEqual(self.call("repo", "unbind"), 0)
        self.assertEqual(self.bound(), {})
        self.assertIn("Warning: Claude local settings were not updated", self.stderr.getvalue())
        self.assertIn(".vkm", self.stderr.getvalue())

    def test_bind_without_labels_asks_for_the_missing_required_ones(self):
        self.use(*CHOICES)
        with self.terminal("2\n"):
            self.assertEqual(self.call("repo", "bind"), 0)
            self.assertEqual(self.bound(), {"customer": "globex"})
            self.assertEqual(self.call("repo", "bind"), 0)
        self.assertIn("Every required label has a value", self.stdout.getvalue())
        self.unbind()
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout), \
                patch("virtual_key_mapper.cli.sys.stdin", io.StringIO("1\n")):
            self.assertEqual(self.call("repo", "bind"), 1)
        self.assertIn("pass --label KEY=VALUE for customer", self.stderr.getvalue())

    def test_bind_without_labels_asks_only_for_what_the_binding_may_set(self):
        team = LabelDefinition("team", required=True, sources=("explicit", "file"))
        self.use(LabelDefinition("customer", required=True, sources=("explicit", "local")), team)
        with self.terminal("acme\n", []):
            self.assertEqual(self.call("repo", "bind"), 0, self.stderr.getvalue())
        self.assertEqual(self.bound(), {"customer": "acme"})
        self.assertIn("'team' cannot be set with a personal binding", self.stderr.getvalue())
        with self.terminal("x\n", []):
            self.assertEqual(self.call("repo", "bind"), 1)  # Only team is missing, and bindings cannot set it.
            self.assertIn("Nothing to ask", self.stderr.getvalue())
            self.assertEqual(self.call("repo", "bind", "--shared"), 0, self.stderr.getvalue())
        self.assertEqual((self.work / ".vkm").read_text(encoding="utf-8").splitlines()[-1], 'team = "x"')
        self.unbind()
        (self.work / ".vkm").unlink()
        self.use(LabelDefinition("customer", required=True, sources=("explicit", "local")))
        with self.terminal("x\n", []):
            self.assertEqual(self.call("repo", "bind", "--shared"), 1)  # customer may not come from a .vkm file.
        self.assertFalse((self.work / ".vkm").exists())
        self.assertEqual(self.bound(), {})
        self.assertEqual(self.bound(), {})

    def test_repo_show_reports_missing_labels_without_secrets(self):
        result, shown = self.show(self.checkout)
        self.assertEqual((result, shown["complete"], shown["headers"], shown["missing"]), (0, False, {}, ["customer"]))
        self.assertTrue(shown["message"].startswith(MISSING_PHRASE))
        self.assertIn("repo bind --shared", shown["message"])
        self.assertEqual(self.stderr.getvalue(), "")
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout):
            self.assertEqual(self.call("repo", "show"), 1)
        self.assertIn("customer (required): no value", self.stdout.getvalue())
        self.assertIn(f"Error: {MISSING_PHRASE} customer for repository {self.repository}", self.stderr.getvalue())

    def test_shared_file_rules_bindings_environment_and_labels_resolve_in_order(self):
        self.assertEqual(self.call("profile", "update", "consulting", "--no-label", "customer", "--rules", str(self.rules_file())), 0)
        result, shown = self.show(self.checkout)
        self.assertEqual((result, shown["labels"][0]["value"], shown["labels"][0]["source"]), (0, "central", "rules"))
        self.assertEqual(shown["labels"][0]["name"], "From rules")
        self.assertEqual(shown["headers"]["x-litellm-tags"], "customer:central")
        self.assertEqual(json.loads(shown["headers"]["x-litellm-spend-logs-metadata"]), {
            "labels": {"customer": "central"}, "sources": {"customer": "rules"}, "repository": self.repository,
        })
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout):
            self.assertEqual(self.call("repo", "bind", "--shared", "--label", "customer=file", "--label", "project=web"), 0)
        self.assertEqual(
            (self.work / ".vkm").read_text(encoding="utf-8").splitlines()[-2:], ['customer = "file"', 'project = "web"'],
        )
        self.assertEqual(self.show(self.checkout)[1]["labels"][0]["source"], "file")
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout):
            self.assertEqual(self.call("repo", "bind", "--label", "customer=mine"), 0)
            self.assertEqual(self.call("repo", "bind", "--shared", "--label", "customer=other"), 0)
        self.assertIn("'customer' still comes from local", self.stderr.getvalue())
        self.assertEqual(self.show(self.checkout)[1]["labels"][0]["value"], "mine")
        with patch.dict(os.environ, {"VKM_LABEL_CUSTOMER": "env", "VKM_LABEL_TEAM": "x"}):
            shown = self.show(self.checkout)[1]
            self.assertEqual(shown["labels"][0]["value"], "env")
            self.assertEqual(shown["ignored"], [
                {"key": "VKM_LABEL_TEAM", "value": "x", "source": "env", "detail": "VKM_LABEL_TEAM", "reason": "unknown label"},
            ])
            self.assertEqual(self.show(self.checkout, "--label", "customer=cli")[1]["labels"][0]["value"], "cli")
            with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout):
                self.assertEqual(self.call("repo", "show"), 0)
        self.assertEqual(self.stdout.getvalue().splitlines(), [
            f"Repository: {self.repository}",
            'customer (required) = "env"  [env: VKM_LABEL_CUSTOMER]',
            f'project = "web"  [file: {self.work / ".vkm"}]',
            'Ignored: VKM_LABEL_TEAM = "x"  [env: VKM_LABEL_TEAM]: unknown label',
        ])

    def test_launch_marker_counts_as_explicit_only_with_launch(self):
        RepositoryStore(self.store).bind(self.profile.gateway, self.repository, {"customer": "mine"})
        with patch.dict(os.environ, {LAUNCH_VARIABLE: json.dumps({"customer": "launched", "team": "x"})}):
            self.assertEqual(self.show(self.checkout)[1]["labels"][0]["value"], "mine")
            shown = self.show(self.checkout, "--launch")[1]
            self.assertEqual((shown["labels"][0]["value"], shown["labels"][0]["source"]), ("launched", "explicit"))
            self.assertEqual([item["key"] for item in shown["ignored"]], ["team"])
            shown = self.show(self.checkout, "--launch", "--label", "customer=cli")[1]
            self.assertEqual(shown["labels"][0]["value"], "cli")
        with patch.dict(os.environ, {LAUNCH_VARIABLE: "{broken"}):
            self.assertEqual(self.show(self.checkout, "--launch")[1]["labels"][0]["value"], "mine")

    def test_repo_show_json_exits_1_only_when_the_configuration_cannot_be_used(self):
        self.use(CHOICES[0], CHOICES[1])
        for broken, environ, expected in (
            ("[labels]\ncustomer = 'a,b'\n", {}, ".vkm"), ("[labels]\n", {"VKM_LABEL_CUSTOMER": "a,b"}, "VKM_LABEL_CUSTOMER"),
            ("[labels]\ncustomer = 'unlisted'\n", {}, "not one of the values"),
            ("[labels]\n", {LAUNCH_VARIABLE: json.dumps({"customer": "unlisted"})}, "not one of the values"),
        ):
            (self.work / ".vkm").write_text(broken, encoding="utf-8")
            with self.subTest(expected=expected), patch.dict(os.environ, environ):
                result, shown = self.show(self.checkout, "--launch")
                self.assertEqual(result, 0)
                self.assertEqual(
                    (shown["complete"], shown["headers"], shown["repository"], shown["missing"]),
                    (False, {}, self.repository, []),
                )
                self.assertIn(expected, shown["message"])
                self.assertEqual([label["key"] for label in shown["labels"]], ["customer", "project"])
        self.assertEqual(self.show(self.checkout, "--label", "team=x"), (1, None))
        self.assertIn("No label 'team'", self.stderr.getvalue())
        self.store.path.write_text("{", encoding="utf-8")
        self.assertEqual(self.show(self.checkout), (1, None))

    def test_shared_file_labels_folders_outside_git(self):
        with patch("virtual_key_mapper.cli.find_checkout", return_value=None), \
                patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
            self.assertEqual(self.call("repo", "bind", "--label", "customer=A"), 1)
            self.assertIn("--shared", self.stderr.getvalue())
            self.assertEqual(self.call("repo", "bind", "--shared", "--label", "customer=A"), 0)
        shown = self.show()[1]
        self.assertEqual((shown["labels"][0]["value"], shown["repository"]), ("A", None))
        self.assertEqual(
            shown["headers"]["x-litellm-spend-logs-metadata"], '{"labels":{"customer":"A"},"sources":{"customer":"file"}}',
        )

    def test_shared_file_in_or_above_home_is_refused_instead_of_silently_ignored(self):
        home = self.home / "users" / "me"  # Its parent stays inside the sandbox even if the refusal regresses.
        home.mkdir(parents=True)
        for root in (home, home.parent):
            with self.subTest(root=root), patch("virtual_key_mapper.resolve.Path.home", return_value=home), \
                    patch("virtual_key_mapper.cli.find_checkout", return_value=Checkout(root, self.repository, None)), \
                    patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError):
                self.assertEqual(self.call("repo", "bind", "--shared", "--label", "customer=A"), 1)
                self.assertIn("would be ignored", self.stderr.getvalue())
                self.assertFalse((root / ".vkm").exists())

    def test_labels_respect_their_allowed_sources(self):
        self.use(LabelDefinition("customer", required=True, sources=("file", "rules")))
        with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout):
            for args in (["repo", "bind", "--label", "customer=A"], ["repo", "show", "--label", "customer=A"]):
                with self.subTest(args=args):
                    self.assertEqual(self.call(*args), 1)
                    self.assertIn("its sources are: file, rules", self.stderr.getvalue())
            self.assertEqual(self.call("repo", "bind", "--shared", "--label", "customer=A"), 0)
        with self.terminal("") as (launch, _):
            self.assertEqual(self.call("run", "claude"), 0)
        self.assertEqual(launch.call_args.args[2].values, {"customer": "A"})

    # vkm labels values

    def test_label_values_come_from_the_definition_or_the_catalogue(self):
        with patch("virtual_key_mapper.cli.NativeKeyring"), patch("virtual_key_mapper.cli.auth_for") as manager, \
                patch("virtual_key_mapper.gateways.litellm.list_tags", return_value=[
                    Choice("customer:A", description="Customer A"), Choice("other"),
                ]) as listed:
            manager.return_value.credential.return_value = Credential("secret")
            self.assertEqual(self.call("labels", "values", "customer", "--json"), 0)
            self.assertEqual(json.loads(self.stdout.getvalue()), {
                "key": "customer", "source": "catalogue", "values": [{"id": "A", "name": None, "description": "Customer A"}],
            })
            listed.assert_called_once_with(self.profile, "secret")
            self.use(*CHOICES)
            self.assertEqual(self.call("labels", "values", "customer"), 0)
            self.assertEqual(self.stdout.getvalue(), '"acme"\t"Acme"\n"globex"\n')  # No name: the id alone, not null.
            self.assertEqual(self.call("labels", "values", "team"), 1)
            self.use(LABELS[0], outputs={"tags": OutputOverride(None, False)})
            self.assertEqual(self.call("labels", "values", "customer"), 0)
        self.assertIn("any valid value is accepted", self.stderr.getvalue())
        self.assertEqual(listed.call_count, 1)

    def test_without_a_catalogue_values_and_the_picker_need_no_sign_in(self):
        self.use(LABELS[0], outputs={"tags": OutputOverride(None, False)})
        with patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError), \
                patch("virtual_key_mapper.cli.auth_for", side_effect=AssertionError), \
                patch("virtual_key_mapper.gateways.litellm.list_tags", side_effect=AssertionError):
            self.assertEqual(self.call("labels", "values", "customer", "--json"), 0)
            self.assertEqual(json.loads(self.stdout.getvalue()), {"key": "customer", "source": None, "values": None})
            stdin = io.StringIO("typed\n")
            with patch("virtual_key_mapper.cli.find_checkout", return_value=self.checkout), \
                    patch("virtual_key_mapper.cli.sys.stdin", stdin), patch.object(stdin, "isatty", return_value=True):
                self.assertEqual(self.call("repo", "bind"), 0, self.stderr.getvalue())
        self.assertEqual(self.bound(), {"customer": "typed"})

    def test_terminal_control_characters_in_the_catalogue_are_escaped(self):
        with patch("virtual_key_mapper.cli.NativeKeyring"), patch("virtual_key_mapper.cli.auth_for"), \
                patch("virtual_key_mapper.gateways.litellm.list_tags", return_value=[
                    Choice("customer:A", description="\x1b[31msecret-looking\ntext"),
                ]):
            self.assertEqual(self.call("labels", "values", "customer"), 0)
        self.assertNotIn("\x1b", self.stdout.getvalue())
        self.assertIn("\\u001b", self.stdout.getvalue())

    def test_the_customer_commands_and_flags_are_gone(self):
        for args in (["tags", "list"], ["run", "--customer", "a", "claude"], ["repo", "bind", "--tag-prefix", "x"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--home", str(self.home), *args])


if __name__ == "__main__":
    unittest.main()
