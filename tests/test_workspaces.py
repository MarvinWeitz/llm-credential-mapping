import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.cli import main
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.profiles import ProfileStore
from llm_credential_mapping.workspaces import WorkspaceBinding, WorkspaceStore


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "config"
        self.store = WorkspaceStore(ProfileStore(self.home))
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.a = self.workspace / "a"
        self.b = self.workspace / "b"
        for directory in (self.a, self.b):
            directory.mkdir()
            subprocess.run(["git", "init", "--quiet", str(directory)], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(directory), "remote", "add", "origin", f"git@github.com:fixture/{directory.name}.git"],
                check=True, capture_output=True,
            )

    def test_primary_checkout_is_explicit_local_and_persists(self):
        binding = self.store.bind(self.workspace, self.a)
        self.assertEqual(binding, WorkspaceBinding(str(self.workspace), str(self.a), "github.com/fixture/a"))
        self.assertEqual(WorkspaceStore(ProfileStore(self.home)).get(self.workspace), binding)
        self.assertEqual(self.store.list(), [binding])
        self.assertFalse((self.workspace / "workspaces.json").exists())
        self.assertTrue((self.home / "workspaces.json").exists())

    def test_no_single_repo_or_parent_prefix_is_guessed(self):
        self.assertIsNone(self.store.get(self.workspace))
        self.store.bind(self.workspace, self.a)
        self.assertIsNone(self.store.get(self.a))
        self.assertIsNone(self.store.get(self.b))

    def test_equivalent_directory_spellings_share_a_selection(self):
        binding = self.store.bind(self.workspace, self.a)
        self.assertEqual(self.store.get(self.workspace / ".." / self.workspace.name), binding)
        if os.name == "nt":
            self.assertEqual(self.store.get(Path(str(self.workspace).upper())), binding)

    def test_rebinding_does_not_mutate_an_existing_snapshot(self):
        a = self.store.bind(self.workspace, self.a)
        b = self.store.bind(self.workspace, self.b)
        self.assertEqual(a.repository, "github.com/fixture/a")
        self.assertEqual(self.store.get(self.workspace), b)
        self.store.unbind(self.workspace)
        self.assertIsNone(self.store.get(self.workspace))
        with self.assertRaises(CredentialError):
            self.store.unbind(self.workspace)

    def test_distinct_workspace_directories_can_select_different_repositories(self):
        another_workspace = self.root / "another-workspace"
        another_workspace.mkdir()
        first = self.store.bind(self.workspace, self.a)
        second = self.store.bind(another_workspace, self.b)
        self.assertEqual(self.store.get(self.workspace), first)
        self.assertEqual(self.store.get(another_workspace), second)

    def test_concurrent_bindings_do_not_lose_another_workspace_selection(self):
        another_workspace = self.root / "another-workspace"
        another_workspace.mkdir()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.store.bind, self.workspace, self.a),
                executor.submit(self.store.bind, another_workspace, self.b),
            ]
            for future in futures:
                future.result()
        self.assertEqual(len(self.store.list()), 2)

    def test_nonexistent_workspace_or_checkout_and_invalid_bindings_are_rejected(self):
        for workspace, directory in ((self.root / "missing", self.a), (self.workspace, self.root / "missing")):
            with self.subTest(workspace=workspace, directory=directory), self.assertRaises(CredentialError):
                self.store.bind(workspace, directory)
        self.assertFalse(self.store.path.exists())
        for values in (("relative", str(self.a), "repo"), (str(self.workspace), "relative", "repo"), (str(self.workspace), str(self.a), "a\nb")):
            with self.subTest(values=values), self.assertRaises(CredentialError):
                WorkspaceBinding(*values)

    def test_failed_atomic_replace_keeps_the_old_primary_selection(self):
        original = self.store.bind(self.workspace, self.a)
        with patch("llm_credential_mapping.profiles.os.replace", side_effect=OSError("full")), self.assertRaises(CredentialError):
            self.store.bind(self.workspace, self.b)
        self.assertEqual(self.store.get(self.workspace), original)

    def test_corrupt_workspace_config_is_not_silently_overwritten(self):
        self.home.mkdir()
        for value in ("{", '{"version":true,"bindings":{}}', '{"version":1,"bindings":{"x":{}}}'):
            self.store.path.write_text(value)
            with self.subTest(value=value), self.assertRaises(CredentialError):
                self.store.bind(self.workspace, self.a)
            self.assertEqual(self.store.path.read_text(), value)

    def test_workspace_cli_needs_neither_login_nor_authentication_profile(self):
        def call(*arguments):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--home", str(self.home), "claude", "workspace", *arguments])
            return result, stdout.getvalue(), stderr.getvalue()

        with patch("llm_credential_mapping.claude.NativeKeyring", side_effect=AssertionError), \
                patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError):
            result, output, errors = call("bind", "--workspace", str(self.workspace), "--repository", str(self.a))
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output)["repository"], "github.com/fixture/a")
            self.assertIn("Reload", errors)
            result, output, errors = call("list")
            self.assertEqual(result, 0)
            self.assertEqual(len(json.loads(output)), 1)
            result, output, errors = call("unbind", "--workspace", str(self.workspace))
            self.assertEqual(result, 0)
        self.assertFalse((self.home / "profiles.json").exists())
