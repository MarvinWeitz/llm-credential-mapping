"""Runs inside a dev container in CI (ci/devcontainer): agents get the host's sign-in through the relay.

The CI host signs in with its real OS vault and runs the companion's request handler; inside the
container the relay (the main extension's code) listens on the CLI's socket and has created a
"forwarded" profile from the host's one. These tests then use the CLI and real agents in the
container, against the host's fake gateway (shared network), and check that only short-lived
access tokens ever reach the container.

Opt-in: LLM_CREDENTIAL_MAPPING_TEST_DEVCONTAINER=1, plus LLMC_GATEWAY (the fake gateway's origin) and
the agent binaries from ci/agents/locate.mjs.
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from live import binary_tests

ACCESS_TOKEN = "devcontainer-access-token"
REFRESH_TOKEN = "devcontainer-refresh-secret"
CUSTOMER = "customer:container"


@binary_tests("devcontainer", "LLM_CREDENTIAL_MAPPING_TEST_DEVCONTAINER")
class DevContainerTests(unittest.TestCase):
    def setUp(self):
        self.gateway = os.environ["LLMC_GATEWAY"]
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Codex clones plugins in the background.
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".llm-customer").write_text(f'customer = "{CUSTOMER}"\n', encoding="utf-8")
        self.environment = {
            **{key: value for key, value in os.environ.items() if not key.startswith(("CLAUDE_CODE_", "ANTHROPIC_", "CODEX_", "OPENAI_"))},
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"), "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }

    def cli(self, *args, directory=None):
        result = subprocess.run(
            [sys.executable, "-m", "llm_credential_mapping", *args], cwd=directory, env=self.environment,
            capture_output=True, text=True, timeout=180, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def run_tool(self, argv, environment=None):
        result = subprocess.run(
            argv, cwd=self.repo, env={**self.environment, **(environment or {}), "PWD": str(self.repo)},
            capture_output=True, text=True, timeout=300, check=False,
        )
        return result.stdout + result.stderr

    def calls(self, suffix):
        with urllib.request.urlopen(self.gateway + "/__calls", timeout=10) as response:
            return [call["headers"] for call in json.load(response) if call["path"].endswith(suffix)]

    def assert_relayed_and_tagged(self, headers, output):
        self.assertTrue(headers, output)
        last = headers[-1]
        self.assertEqual(last.get("authorization"), "Bearer " + ACCESS_TOKEN, output)
        self.assertEqual(last.get("x-litellm-tags"), CUSTOMER, output)
        self.assertEqual(json.loads(last["x-litellm-spend-logs-metadata"])["customer_source"], "file")

    def test_the_relay_created_a_forwarded_profile_from_the_hosts(self):
        shown = json.loads(self.cli("profile", "show", "--json"))
        self.assertEqual(shown["auth"]["type"], "forwarded")
        self.assertEqual(shown["gateway"]["url"], self.gateway)

    def test_tokens_come_from_the_host_vault_through_the_relay(self):
        self.assertEqual(self.cli("token").strip(), ACCESS_TOKEN)
        self.assertEqual(json.loads(self.cli("token", "--json"))["access_token"], ACCESS_TOKEN)

    def test_codex_in_the_container_uses_the_relayed_credential_and_the_folders_customer(self):
        self.cli("setup", "codex", "--model", "alias")
        exported = {}
        for line in self.cli("repo", "show", "--env", "bash", "--directory", str(self.repo)).splitlines():
            if line.startswith("export "):
                name, _, value = line.removeprefix("export ").partition("=")
                exported[name] = shlex.split(value)[0]
        before = len(self.calls("/responses"))
        output = self.run_tool(
            [os.environ["LLM_CREDENTIAL_MAPPING_TEST_CODEX_BINARY"], "exec", "--skip-git-repo-check", "Say OK"], exported,
        )
        self.assertIn("FIXTURE_OK", output)
        self.assert_relayed_and_tagged(self.calls("/responses")[before:], output)

    def test_claude_in_the_container_uses_the_relayed_credential_and_the_folders_customer(self):
        self.cli("setup", "claude")
        claude = [os.environ["LLM_CREDENTIAL_MAPPING_TEST_CLAUDE_BINARY"], "-p", "Say OK", "--output-format", "json"]
        self.run_tool(claude)  # The guard assigns the folder's customer and blocks this first prompt once.
        before = len(self.calls("/messages"))
        output = self.run_tool(claude)
        self.assertIn("FIXTURE_OK", output)
        self.assert_relayed_and_tagged(self.calls("/messages")[before:], output)

    def test_the_refresh_grant_never_reaches_the_container(self):
        self.cli("token")
        homes = [Path.home(), self.root, Path(tempfile.gettempdir())]
        for home in homes:
            for path in home.rglob("*"):
                if path.is_file() and path.stat().st_size < 10_000_000:
                    try:
                        content = path.read_bytes()
                    except OSError:
                        continue
                    self.assertNotIn(REFRESH_TOKEN.encode(), content, str(path))
