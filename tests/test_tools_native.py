"""Optional: real Codex and OpenCode binaries against a local fake gateway, configured by setup and run.

Set LLM_CREDENTIAL_MAPPING_TEST_CODEX_BINARY and/or LLM_CREDENTIAL_MAPPING_TEST_OPENCODE_BINARY. Every tool
folder is temporary. Only the credential step is a stub (a real token needs a signed-in vault): for Codex
it enforces the customer variables like 'token --require-attribution-env'; for OpenCode it answers
'token --json' and passes everything else (repo show) to the real CLI.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.adapters import Credential, IntegrationContext
from llm_credential_mapping.cli import main
from llm_credential_mapping.customers import Attribution
from llm_credential_mapping.integrations.codex import Codex
from llm_credential_mapping.integrations.common import header_variable
from llm_credential_mapping.integrations.opencode import OpenCode
from llm_credential_mapping.profiles import ProfileStore
from llm_credential_mapping.registry import gateway_for
from llm_credential_mapping.shells import assignments
from live import binary_tests

STUB = """
import json, os, subprocess, sys
from pathlib import Path
counter = Path(__file__).with_suffix(".calls")
args = sys.argv[1:]
if "token" not in args:
    sys.exit(subprocess.run(args).returncode)
if "--require-attribution-env" in args and not all(os.environ.get(name) for name in REQUIRED):
    sys.stderr.write("No customer for this session")
    sys.exit(1)
with counter.open("a") as calls_file:  # Tools ask concurrently: an append never reads a half-written file.
    calls_file.write("x")
calls = counter.stat().st_size
print(json.dumps({"access_token": f"token-{calls}", "expires_at": None}) if "--json" in args else f"token-{calls}")
"""


class FakeGateway:
    def __init__(self):
        self.calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, raw: bytes, content_type: str, status: int = 200):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.startswith("/v1/models"):
                    self.send(json.dumps({"object": "list", "data": [{"id": "alias", "object": "model"}]}).encode(), "application/json")
                else:
                    self.send(b"{}", "application/json", 404)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.calls.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
                if self.path.endswith("/chat/completions"):
                    chunks = [
                        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "alias",
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "FIXTURE_OK"}, "finish_reason": None}]},
                        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "alias",
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
                    ]
                    raw = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                else:
                    events = [
                        {"type": "response.created", "response": {"id": "r"}},
                        {"type": "response.output_item.done", "item": {
                            "type": "message", "role": "assistant", "id": "m",
                            "content": [{"type": "output_text", "text": "FIXTURE_OK"}]}},
                        {"type": "response.completed", "response": {"id": "r", "usage": {
                            "input_tokens": 5, "input_tokens_details": None, "output_tokens": 2,
                            "output_tokens_details": None, "total_tokens": 7}}},
                    ]
                    raw = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
                self.send(raw.encode(), "text/event-stream")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def model_calls(self):
        return [headers for path, headers in self.calls if path.endswith(("/responses", "/chat/completions"))]


class NativeToolCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Codex clones plugins in the background.
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)
        self.home = self.root / "credentials"
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("OPENAI", "CODEX", "OPENCODE", "LLM_CREDENTIALS", "XDG_"))
        }
        self.environment.update({
            "CODEX_HOME": str(self.root / "codex-home"), "OPENCODE_DISABLE_AUTOUPDATE": "1",
            **{name: str(self.root / name.lower()) for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")},
        })
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".llm-customer").write_text('customer = "customer:live"\n', encoding="utf-8")
        self.unmapped = self.root / "unmapped"
        self.unmapped.mkdir()
        self.cli("profile", "add", "native", "--gateway", self.gateway.origin, "--team-id", "t", "--allow-insecure-localhost")
        self.profile = ProfileStore(self.home).get("native")
        required = [header_variable(name) for name in gateway_for(self.profile).attribution_header_names()]
        self.stub = self.root / "stub.py"
        self.stub.write_text("REQUIRED = " + json.dumps(required) + "\n" + STUB, encoding="utf-8")

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch.dict(os.environ, self.environment, clear=True), \
                patch("llm_credential_mapping.cli.NativeKeyring"), \
                patch("llm_credential_mapping.cli.auth_for") as auth, \
                patch("llm_credential_mapping.customers.Path.home", return_value=self.root):
            auth.return_value.credential.return_value = Credential("setup-token")
            self.assertEqual(main(["--home", str(self.home), *args]), 0)

    def stub_context(self) -> IntegrationContext:
        return IntegrationContext(
            self.profile, gateway_for(self.profile), "unused", self.home, cli=(sys.executable, str(self.stub), sys.executable, "-I", "-m", "llm_credential_mapping"),
        )

    def run_tool(self, argv, directory, environment=None):
        self.gateway.calls.clear()
        # Like a shell: PWD matches the working directory. OpenCode takes its directory from $PWD,
        # so the test runner's own PWD would otherwise leak in.
        env = {**self.environment, **(environment or {}), "PWD": str(directory)}
        result = subprocess.run(
            argv, cwd=directory, env=env, capture_output=True, text=True,
            timeout=300, check=False,
        )
        return result.stdout + result.stderr

    def assert_tagged(self, output, customer="customer:live"):
        calls = self.gateway.model_calls()
        self.assertTrue(calls, output)
        for headers in calls:
            self.assertTrue(headers.get("authorization", "").startswith("Bearer token-"), output)
            self.assertEqual(headers["x-litellm-tags"], customer, output)
            self.assertIn("customer_source", headers["x-litellm-spend-logs-metadata"])


@binary_tests("codex", "LLM_CREDENTIAL_MAPPING_TEST_CODEX_BINARY")
class NativeCodexTests(NativeToolCase):
    def setUp(self):
        super().setUp()
        self.binary = os.environ["LLM_CREDENTIAL_MAPPING_TEST_CODEX_BINARY"]
        self.cli("setup", "codex", "--model", "alias")
        # Replace only the token command; everything else stays as setup wrote it.
        path = Path(self.environment["CODEX_HOME"]) / "config.toml"
        auth = tomllib.loads(path.read_text(encoding="utf-8"))["model_providers"]["llm-credentials"]["auth"]
        text = path.read_text(encoding="utf-8").replace(
            "args = " + json.dumps(auth["args"]), "args = " + json.dumps([str(self.stub), *([auth["command"]] + auth["args"])]),
        )
        path.write_text(text, encoding="utf-8")

    def shell_environment(self, directory):
        """What the shell-init hook exports in a directory (via the real 'repo show --env')."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch.dict(os.environ, self.environment, clear=True), \
                patch("llm_credential_mapping.customers.Path.home", return_value=self.root):
            main(["--home", str(self.home), "repo", "show", "--env", "bash", "--directory", str(directory)])
        values = {}
        for line in stdout.getvalue().splitlines():
            if line.startswith("export "):
                name, _, value = line.removeprefix("export ").partition("=")
                values[name] = __import__("shlex").split(value)[0]
        return values

    def test_setup_with_the_shell_hook_tags_requests(self):
        output = self.run_tool([self.binary, "exec", "--skip-git-repo-check", "Say OK"], self.repo, self.shell_environment(self.repo))
        self.assertIn("FIXTURE_OK", output)
        self.assert_tagged(output)

    @unittest.skipIf(os.name == "nt", "interactive Codex needs a pseudo-terminal")
    def test_interactive_codex_ignores_a_daemon_started_with_another_customer(self):
        # Codex's shared daemon keeps the environment of the terminal that started it; the shell hook's
        # codex function and 'run codex' start Codex with --no-daemon so the folder's customer applies.
        environment = {**self.environment, **self.shell_environment(self.repo), "PWD": str(self.repo)}
        other = {name: value.replace("customer:live", "customer:other") for name, value in environment.items()}
        daemon = [self.binary, "app-server", "daemon"]
        subprocess.run([*daemon, "start"], cwd=self.root, env=other, capture_output=True, timeout=60, check=False)
        self.addCleanup(subprocess.run, [*daemon, "stop"], env=other, capture_output=True, timeout=60, check=False)
        self.gateway.calls.clear()
        screen = self.interactive([self.binary, "--no-daemon"], environment, "Say OK")
        self.assert_tagged(screen)

    def interactive(self, argv, environment, prompt):
        """Type a prompt into Codex's terminal UI and wait for its request; returns the screen output."""
        import pty
        import select
        import time

        # The pseudo-terminal is opened here and Codex started by subprocess: forking this threaded
        # process (the fake gateway) with pty.fork() can deadlock the child.
        terminal, child = pty.openpty()
        process = subprocess.Popen(
            argv, cwd=self.repo, stdin=child, stdout=child, stderr=child, start_new_session=True,
            env={**environment, "TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40"},
        )
        os.close(child)
        screen = b""

        def read(seconds, until=lambda: False):
            nonlocal screen
            deadline = time.time() + seconds
            while time.time() < deadline and not until():
                if select.select([terminal], [], [], 0.2)[0]:
                    try:
                        data = os.read(terminal, 65536)
                    except OSError:
                        return
                    screen += data
                    if b"\x1b[6n" in data:  # The UI asks for the cursor position.
                        os.write(terminal, b"\x1b[1;1R")

        try:
            read(8)
            os.write(terminal, prompt.encode())
            read(1)
            os.write(terminal, b"\r")
            read(60, lambda: bool(self.gateway.model_calls()))
        finally:
            process.kill()
            process.wait(timeout=30)
            os.close(terminal)
        return screen.decode("utf-8", "replace")

    def test_without_a_customer_codex_gets_no_credential(self):
        self.assertEqual(self.shell_environment(self.unmapped), {})
        output = self.run_tool([self.binary, "exec", "--skip-git-repo-check", "Say OK"], self.unmapped)
        self.assertIn("No customer for this session", output)
        for headers in self.gateway.model_calls():
            self.assertNotIn("authorization", headers)
            self.assertNotIn("x-litellm-tags", headers)

    def test_run_passes_its_own_customer(self):
        argv, env = Codex().command(self.stub_context(), Attribution("customer:cli", "explicit", "--customer"),
                                    ["exec", "--skip-git-repo-check", "Say OK"], "alias")
        output = self.run_tool([self.binary, *argv[1:]], self.unmapped, {k: v for k, v in env.items() if k.startswith("LLM_")})
        self.assert_tagged(output, "customer:cli")


@binary_tests("opencode", "LLM_CREDENTIAL_MAPPING_TEST_OPENCODE_BINARY")
class NativeOpenCodeTests(NativeToolCase):
    def setUp(self):
        super().setUp()
        self.binary = os.environ["LLM_CREDENTIAL_MAPPING_TEST_OPENCODE_BINARY"]
        with patch.dict(os.environ, {"PATH": str(Path(self.binary).parent) + os.pathsep + os.environ.get("PATH", "")}):
            self.cli("setup", "opencode", "--model", "alias")
        self.stub_plugin()

    def stub_plugin(self):
        plugin = self.home / "opencode" / "llm-credentials-native.js"
        text = plugin.read_text(encoding="utf-8")
        settings = json.loads(text.split("const SETTINGS = ", 1)[1].split(";\n", 1)[0])
        stubbed = {**settings, "cli": [sys.executable, str(self.stub), *settings["cli"]]}
        plugin.write_text(text.replace(json.dumps(settings), json.dumps(stubbed)), encoding="utf-8")

    def test_setup_tags_requests_with_the_folders_customer(self):
        output = self.run_tool([self.binary, "run", "Say OK"], self.repo)
        self.assertIn("FIXTURE_OK", output)
        self.assert_tagged(output)

    def test_unmapped_folders_fail_before_sending(self):
        output = self.run_tool([self.binary, "run", "Say OK"], self.unmapped)
        self.assertEqual(self.gateway.model_calls(), [], output)
        self.assertIn("No customer is assigned", output)

    def test_run_passes_its_own_customer(self):
        argv, env = OpenCode().command(self.stub_context(), Attribution("customer:cli", "explicit", "--customer"),
                                       ["run", "Say OK"], "alias")
        self.stub_plugin()
        environment = {k: v for k, v in env.items() if k.startswith(("LLM_", "OPENCODE_CONFIG_CONTENT"))}
        output = self.run_tool([self.binary, *argv[1:]], self.unmapped, environment)
        self.assert_tagged(output, "customer:cli")
