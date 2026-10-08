import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from llm_credential_mapping.auth.litellm_sso import vault_account
from llm_credential_mapping.claude import ClaudeContext, wrapped_command
from llm_credential_mapping.auth.litellm_oauth import Session
from test_sessions import make_session
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.storage import NativeKeyring
from test_oauth import discovery


@unittest.skipUnless(os.environ.get("LLM_CREDENTIAL_MAPPING_TEST_CLAUDE_BINARY"), "Optional installed Claude/vault integration")
class NativeClaudeTests(unittest.TestCase):
    def test_official_binary_uses_pinned_headers_and_native_vault_helper(self):
        binary = Path(os.environ["LLM_CREDENTIAL_MAPPING_TEST_CLAUDE_BINARY"])
        self.assertTrue(binary.is_file())
        calls = []
        refreshes = []
        rejected_first = threading.Event()

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
                if self.path == "/.well-known/litellm-cli-auth":
                    self.respond(discovery(profile.gateway))
                else:
                    self.respond({"error": "not-found"}, 404)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/token":
                    form = {name: values[0] for name, values in parse_qs(raw.decode()).items()}
                    refreshes.append(form)
                    self.respond({
                        "token_type": "Bearer", "access_token": "fixture-renewed",
                        "refresh_token": "fixture-rotated", "expires_in": 3600,
                        "user_id": "fixture-employee", "team_id": "fixture-unit",
                    })
                    return
                body = json.loads(raw)
                calls.append((self.path, dict(self.headers), body))
                if self.path.startswith("/v1/messages") and not rejected_first.is_set():
                    rejected_first.set()
                    secrets.put(vault_account(home, profile), replace(session, expires_at=0).to_dict())
                    self.respond({"type": "error", "error": {"type": "authentication_error", "message": "fixture expiry"}}, 401)
                    return
                message = {
                    "id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture-model",
                    "content": [{"type": "text", "text": "FIXTURE_OK"}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                }
                if body.get("stream"):
                    events = [
                        ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}),
                        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "FIXTURE_OK"}}),
                        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}),
                        ("message_stop", {"type": "message_stop"}),
                    ]
                    raw = "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in events).encode()
                    content_type = "text/event-stream"
                else:
                    raw = json.dumps(message).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                primary = workspace / "primary"
                primary.mkdir(parents=True)
                home = root / "credentials"
                store = ProfileStore(home)
                profile = Profile("native-fixture", f"http://127.0.0.1:{server.server_port}", {"team_id": "fixture-unit"}, allow_http=True)
                store.add(profile)
                secrets = NativeKeyring()
                session = make_session(
                    profile, user_id="fixture-employee", client_id="fixture-client",
                    access_token="fixture-access", refresh_token="fixture-refresh",
                )
                secrets.put(vault_account(home, profile), session.to_dict())
                try:
                    context = ClaudeContext(
                        str(home), profile.name, profile.account, session.user_id,
                        "github.com/fixture/repo", "customer:fixture", "fixture-model",
                    )
                    command, env = wrapped_command(str(binary), [
                        "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                        "--project-config-root", str(workspace),
                        "--tools", "", "--setting-sources", "", "--no-session-persistence",
                    ], context, profile, primary)
                    env["CLAUDE_CONFIG_DIR"] = str(root / "isolated-claude")
                    initialize = json.dumps({
                        "type": "control_request", "request_id": "picker-fixture",
                        "request": {"subtype": "initialize", "hooks": {}},
                    }) + "\n"
                    reset_model = json.dumps({
                        "type": "control_request", "request_id": "picker-default-fixture",
                        "request": {"subtype": "set_model"},
                    }) + "\n"
                    message = json.dumps({
                        "type": "user", "message": {"role": "user", "content": "Reply with FIXTURE_OK."},
                        "parent_tool_use_id": None, "session_id": "",
                    }) + "\n"
                    result = subprocess.run(
                        command, env=env, cwd=primary, input=initialize + reset_model + message,
                        capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                    events = [json.loads(line) for line in result.stdout.splitlines()]
                    picker = next(
                        event["response"]["response"]["models"] for event in events
                        if event["type"] == "control_response"
                        and event["response"].get("request_id") == "picker-fixture"
                    )
                    self.assertEqual([model["value"] for model in picker], ["default", "fixture-model"])
                    self.assertEqual(picker[1]["displayName"], "fixture-model")
                    self.assertEqual(picker[1]["description"], "Configured LiteLLM model")
                    self.assertIn("fixture-model", picker[0]["description"])
                    self.assertNotIn("[1m]", picker[0]["description"])
                    reset_response = next(
                        event["response"] for event in events
                        if event["type"] == "control_response"
                        and event["response"].get("request_id") == "picker-default-fixture"
                    )
                    self.assertEqual(reset_response["subtype"], "success", reset_response)
                    initialization = next(
                        event for event in events if event["type"] == "system" and event.get("subtype") == "init"
                    )
                    self.assertEqual(Path(initialization["cwd"]).resolve(), primary.resolve())
                    response = next(event for event in events if event["type"] == "result")
                    self.assertEqual(response["result"], "FIXTURE_OK")
                    model_calls = [call for call in calls if call[0].startswith("/v1/messages")]
                    self.assertGreaterEqual(len(model_calls), 2)
                    for index, (path, headers, body) in enumerate(model_calls):
                        lowered = {key.lower(): value for key, value in headers.items()}
                        expected = "fixture-access" if index == 0 else "fixture-renewed"
                        self.assertEqual(lowered["x-litellm-tags"], "customer:fixture")
                        self.assertEqual(lowered["authorization"], "Bearer " + expected)
                        self.assertEqual(lowered["x-api-key"], expected)
                        self.assertEqual(body["model"], "fixture-model")
                    self.assertEqual(len(refreshes), 1)
                    self.assertEqual(refreshes[0]["grant_type"], "refresh_token")
                    self.assertEqual(refreshes[0]["refresh_token"], "fixture-refresh")
                    self.assertEqual(secrets.get(vault_account(home, profile))["refresh_token"], "fixture-rotated")
                    self.assertNotIn("fixture-access", " ".join(command))
                    self.assertNotIn("fixture-renewed", " ".join(command))
                finally:
                    secrets.delete(vault_account(home, profile))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
