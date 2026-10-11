"""Optional: the real Claude Code binary against a local fake gateway, configured by 'setup claude'.

Set LLM_CREDENTIAL_MAPPING_TEST_CLAUDE_BINARY to the binary (e.g. the one bundled with the VS Code
extension). Uses a temporary CLAUDE_CONFIG_DIR and configuration folder; nothing of yours is changed.
The credential helper is replaced by a stub that prints token-1, then token-2, so no sign-in is needed.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.adapters import Credential, IntegrationContext
from llm_credential_mapping.cli import main
from llm_credential_mapping.customers import Attribution
from llm_credential_mapping.integrations.claude import Claude, settings_path
from llm_credential_mapping.integrations.common import shell_command
from llm_credential_mapping.profiles import ProfileStore
from llm_credential_mapping.registry import gateway_for

HELPER = """
import sys
from pathlib import Path
counter = Path(sys.argv[1])
calls = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(calls))
print(f"token-{calls}")
"""


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.rejected = set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, body, status=200):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.respond({"error": "not-found"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                owner.calls.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
                if self.path.startswith("/v1/messages") and not owner.rejected:
                    # The first token "expired": Claude must ask the helper again and retry.
                    owner.rejected.add(self.headers.get("Authorization"))
                    self.respond({"type": "error", "error": {"type": "authentication_error", "message": "expired"}}, 401)
                    return
                if self.headers.get("Authorization") in owner.rejected:
                    self.respond({"type": "error", "error": {"type": "authentication_error", "message": "expired"}}, 401)
                    return
                if "count_tokens" in self.path:
                    self.respond({"input_tokens": 10})
                    return
                message = {
                    "id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture-model",
                    "content": [{"type": "text", "text": "FIXTURE_OK"}], "stop_reason": "end_turn",
                    "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 2},
                }
                if not body.get("stream"):
                    self.respond(message)
                    return
                events = [
                    ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "FIXTURE_OK"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                raw = "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@unittest.skipUnless(os.environ.get("LLM_CREDENTIAL_MAPPING_TEST_CLAUDE_BINARY"), "Set LLM_CREDENTIAL_MAPPING_TEST_CLAUDE_BINARY")
class NativeClaudeTests(unittest.TestCase):
    def setUp(self):
        self.binary = Path(os.environ["LLM_CREDENTIAL_MAPPING_TEST_CLAUDE_BINARY"])
        self.assertTrue(self.binary.is_file())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)
        self.home = self.root / "credentials"
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "LLM_CREDENTIALS_"))
        }
        self.environment.update({
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "--quiet"], ["remote", "add", "origin", "https://github.com/acme/web"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        self.cli(
            "profile", "add", "native", "--gateway", self.gateway.origin, "--team-id", "t", "--allow-insecure-localhost",
        )
        with patch("llm_credential_mapping.cli.auth_for") as auth:
            auth.return_value.credential.return_value = Credential("unused")
            self.cli("setup", "claude", "--model", "fixture-model")
        helper = self.root / "helper.py"
        helper.write_text(HELPER, encoding="utf-8")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.environment["CLAUDE_CONFIG_DIR"]}):
            path = settings_path()
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["apiKeyHelper"] = shell_command([sys.executable, str(helper), str(self.root / "helper-calls")])
        path.write_text(json.dumps(settings), encoding="utf-8")

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.environment["CLAUDE_CONFIG_DIR"]}):
            self.assertEqual(main(["--home", str(self.home), *args]), 0)

    def claude(self, directory):
        self.gateway.calls.clear()
        result = subprocess.run(
            [str(self.binary), "-p", "Say OK", "--output-format", "json"], cwd=directory, env=self.environment,
            capture_output=True, text=True, timeout=180, check=False,
        )
        return result.stdout + result.stderr

    def model_calls(self):
        return [headers for path, headers in self.gateway.calls if path.startswith("/v1/messages")]

    def test_guard_assignment_tags_and_helper_renewal(self):
        output = self.claude(self.repo)
        self.assertEqual(self.model_calls(), [], output)
        self.assertIn("No customer is assigned", output)

        (self.repo / ".llm-customer").write_text('customer = "customer:live"\n', encoding="utf-8")
        output = self.claude(self.repo)
        self.assertEqual(self.model_calls(), [], output)
        self.assertIn("Send the prompt again", output)

        output = self.claude(self.repo)
        calls = self.model_calls()
        self.assertIn("FIXTURE_OK", output)
        self.assertTrue(calls, output)
        first, last = (int(call["authorization"].removeprefix("Bearer token-")) for call in (calls[0], calls[-1]))
        self.assertGreater(last, first, "Claude asks the helper again after a 401")
        for headers in calls:
            self.assertEqual(headers["x-litellm-tags"], "customer:live")
            self.assertEqual(
                json.loads(headers["x-litellm-spend-logs-metadata"]),
                {"customer_source": "file", "repository": "github.com/acme/web"},
            )

    def test_run_override_passes_the_guard_with_its_own_customer(self):
        profile = ProfileStore(self.home).get("native")
        context = IntegrationContext(
            profile, gateway_for(profile), "", cli=(sys.executable, str(self.root / "helper.py"), str(self.root / "helper-calls")),
        )
        argv, env = Claude().command(
            context, Attribution("customer:cli", "explicit", "--customer"), ["-p", "Say OK", "--output-format", "json"], None,
        )
        env = {**env, **self.environment}
        self.gateway.calls.clear()
        result = subprocess.run(
            [str(self.binary), *argv[1:]], cwd=self.repo, env=env, capture_output=True, text=True, timeout=180, check=False,
        )
        output = result.stdout + result.stderr
        self.assertIn("FIXTURE_OK", output)
        self.assertTrue(self.model_calls(), output)
        self.assertTrue(all(headers["x-litellm-tags"] == "customer:cli" for headers in self.model_calls()), output)
        self.assertFalse((self.repo / ".claude" / "settings.local.json").exists())

    def test_subdirectory_sessions_are_tagged(self):
        (self.repo / ".llm-customer").write_text('customer = "customer:live"\n', encoding="utf-8")
        sub = self.repo / "src" / "deep"
        sub.mkdir(parents=True)
        output = self.claude(sub)
        if self.model_calls() == []:
            self.assertIn("Send the prompt again", output)
            output = self.claude(sub)
        self.assertTrue(self.model_calls(), output)
        self.assertTrue(all(headers["x-litellm-tags"] == "customer:live" for headers in self.model_calls()), output)
