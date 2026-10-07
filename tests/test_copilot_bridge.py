import http.client
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.copilot import CopilotConnection, CopilotStore, connection_key
from llm_credential_mapping.copilot_bridge import CopilotBridge, check, serve
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.litellm import Session
from llm_credential_mapping.profiles import Profile, ProfileStore
from llm_credential_mapping.repositories import RepositoryStore
from llm_credential_mapping.sessions import SessionManager
from test_sessions import MemoryStore, make_session


class CopilotBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.requests = []
        self.logs = []
        self.release_stream = threading.Event()
        self.stream_started = threading.Event()
        self.completion_sent = threading.Event()
        test = self

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.requests.append((self.path, {name.lower(): value for name, value in self.headers.items()}, payload))
                mode = payload.get("metadata", {}).get("mode")
                if mode in ("deny", "redirect", "rate-limit"):
                    self.send_response({"deny": 401, "redirect": 307, "rate-limit": 429}[mode])
                    if mode == "redirect":
                        self.send_header("Location", test.profile.gateway + "/must-not-follow")
                    self.end_headers()
                    self.wfile.write(b'{"error":"access-secret"}')
                    return
                if payload.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    first = b'data: {"id":"fixture","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"}}]}\n\n'
                    self.wfile.write(first)
                    self.wfile.flush()
                    test.stream_started.set()
                    if mode == "hold":
                        test.release_stream.wait(5)
                    if mode == "incomplete":
                        return
                    self.wfile.write(
                        b'data: {"id":"fixture","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call","type":"function","function":{"name":"read_file","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\r\n\r\n'
                        b'data: [DONE]\r\n'
                    )
                    self.wfile.flush()
                    if mode == "split-completion":
                        test.completion_sent.set()
                        test.release_stream.wait(5)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"id":"fixture","choices":[{"message":{"role":"assistant","content":"Hello"},"finish_reason":"stop"}]}')

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        self.gateway.daemon_threads = True
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()
        self.addCleanup(self.close_gateway)
        self.profiles = ProfileStore(self.home / "config")
        self.profile = Profile("unit", f"http://127.0.0.1:{self.gateway.server_port}", "team", True)
        self.profiles.add(self.profile)
        self.repository = "github.com/company/repo"
        RepositoryStore(self.profiles).bind(self.profile.gateway, self.repository, "customer:A")
        self.vault = MemoryStore()
        self.session = make_session(self.profile)
        self.vault.put(self.profile.account, self.session.to_dict())
        self.refreshes = []
        fixture = self

        class Refresh:
            def refresh(self, session):
                fixture.refreshes.append(session.refresh_token)
                return replace(
                    session, access_token="renewed-secret", refresh_token="rotated-secret",
                    issued_at=time.time(), expires_at=time.time() + 3600,
                )

        self.manager = SessionManager(self.profiles, self.vault, lambda profile: Refresh())
        self.connection = CopilotConnection(
            self.profile.name, self.profile.account, "employee", self.repository, "customer:A",
            "model-alias", str(self.home), self.free_port(), 32000, 4096,
        )
        self.connections = CopilotStore(self.profiles)
        self.connections.put(self.connection, self.vault)
        self.key = connection_key(self.connection, self.vault)
        self.bridge = CopilotBridge(
            self.connections, self.connection, self.profile, self.manager, self.key, self.logs.append,
        )
        self.bridge_thread = threading.Thread(target=self.bridge.serve_forever, daemon=True)
        self.bridge_thread.start()
        self.addCleanup(self.close_bridge)

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def close_bridge(self):
        self.release_stream.set()
        self.bridge.shutdown()
        self.bridge.server_close()
        self.bridge_thread.join(5)

    def close_gateway(self):
        self.release_stream.set()
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(5)

    def request(self, payload=None, *, key=None, path=None, method="POST", headers=None, raw=None, headers_only=False):
        client = http.client.HTTPConnection("127.0.0.1", self.connection.port, timeout=5)
        self.addCleanup(client.close)
        body = raw if raw is not None else json.dumps(payload if payload is not None else self.payload()).encode()
        if method in ("GET", "OPTIONS") and raw is None:
            body = None
        values = {
            "Authorization": "Bearer " + (self.key if key is None else key),
            "Content-Type": "application/json",
            **(headers or {}),
        }
        if headers_only:
            values.setdefault("Content-Length", str(len(body or b"")))
            client.request(method, path or self.connection.base_path + "/chat/completions", headers=values)
        else:
            client.request(method, path or self.connection.base_path + "/chat/completions", body=body, headers=values)
        return client.getresponse()

    def payload(self, **values):
        return {"model": "model-alias", "messages": [{"role": "user", "content": "private-prompt"}], **values}

    def test_nonstream_forwarding_pins_authentication_tag_model_and_employee_not_client_overrides(self):
        response = self.request(self.payload(
            user="another", metadata={"tags": ["customer:B"], "team_id": "another", "user_api_key": "wrong", "keep": "yes"},
        ), headers={"x-litellm-tags": "customer:B", "x-extra-secret": "private"})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["choices"][0]["message"]["content"], "Hello")
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["authorization"], "Bearer access-secret")
        self.assertEqual(headers["x-litellm-tags"], "customer:A")
        self.assertNotIn("x-extra-secret", headers)
        self.assertEqual(body["model"], "model-alias")
        self.assertEqual(body["user"], "employee")
        self.assertEqual(body["metadata"], {"keep": "yes"})
        self.assertEqual(body["max_completion_tokens"], 4096)
        self.assertNotIn(self.key, str(self.requests))
        self.assertNotIn("private-prompt", str(self.logs))
        self.assertNotIn("access-secret", str(self.logs))

    def test_sse_is_flushed_before_upstream_finishes_and_tool_calls_are_preserved(self):
        response = self.request(self.payload(stream=True, metadata={"mode": "hold"}))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        first = response.readline()
        self.assertIn(b"Hello", first)
        self.assertFalse(self.release_stream.is_set())
        self.release_stream.set()
        rest = response.read()
        self.assertIn(b'"tool_calls"', rest)
        self.assertIn(b"data: [DONE]", rest)

    def test_expired_grant_rotates_once_and_requests_never_receive_refresh_or_local_keys(self):
        self.vault.put(self.profile.account, replace(self.session, issued_at=time.time() - 3599, expires_at=time.time() + 1).to_dict())
        for _ in range(2):
            response = self.request()
            self.assertEqual(response.status, 200)
            response.read()
        self.assertEqual(self.refreshes, ["refresh-secret"])
        self.assertEqual([item[1]["authorization"] for item in self.requests], ["Bearer renewed-secret"] * 2)
        self.assertEqual(self.vault.get(self.profile.account)["refresh_token"], "rotated-secret")
        self.assertNotIn(self.key, str(self.requests))

    def test_changed_employee_is_rejected_before_refresh_or_upstream_access(self):
        self.vault.put(self.profile.account, replace(self.session, user_id="another", issued_at=time.time() - 3599, expires_at=time.time() + 1).to_dict())
        response = self.request()
        self.assertEqual(response.status, 503)
        self.assertIn("employee changed", json.loads(response.read())["error"]["message"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.refreshes, [])

    def test_global_profile_and_repository_rebind_do_not_retag_a_running_connection(self):
        other = Profile("other", self.profile.gateway, "other-team", True)
        self.profiles.add(other)
        self.profiles.select(other.name)
        RepositoryStore(self.profiles).bind(self.profile.gateway, self.repository, "customer:B", replace=True)
        response = self.request()
        self.assertEqual(response.status, 200)
        response.read()
        self.assertEqual(self.requests[0][1]["x-litellm-tags"], "customer:A")
        self.assertEqual(self.requests[0][1]["authorization"], "Bearer access-secret")

    def test_wrong_local_key_host_origin_and_connection_path_cannot_reach_litellm(self):
        cases = (
            {"key": "wrong"},
            {"headers": {"Host": "attacker.example"}},
            {"headers": {"Origin": "https://attacker.example"}},
            {"path": "/llm-credentials/" + "0" * 64 + "/v1/chat/completions"},
        )
        for options in cases:
            with self.subTest(options=options):
                response = self.request(**options, headers_only=True)
                self.assertIn(response.status, (401, 403, 404))
                response.read()
        self.assertEqual(self.requests, [])
        response = self.request(method="OPTIONS")
        self.assertEqual(response.status, 403)
        response.read()

    def test_route_model_or_gateway_override_fields_are_rejected(self):
        for payload in (
            self.payload(model="other"), self.payload(api_key="attacker"),
            self.payload(api_base="http://attacker.example"), self.payload(messages=[]),
            self.payload(stream="true"), self.payload(metadata=[]),
            self.payload(max_tokens=4097), self.payload(max_completion_tokens=True),
        ):
            with self.subTest(payload=payload):
                response = self.request(payload)
                self.assertEqual(response.status, 400)
                response.read()
        self.assertEqual(self.requests, [])

    def test_invalid_nonfinite_deep_or_oversized_json_requests_are_visible_errors(self):
        for raw in (
            b"{", b'{"x":NaN}', b'{"model":"model-alias","messages":[1],"temperature":1e9999}',
            b'{"x":' + b"[" * 2000 + b"]" * 2000 + b"}",
        ):
            with self.subTest(raw=raw[:50]):
                response = self.request(raw=raw)
                self.assertEqual(response.status, 400)
                response.read()
        response = self.request(headers={"Content-Length": "8388609"}, headers_only=True)
        self.assertEqual(response.status, 413)
        response.read()
        response = self.request(headers={"Content-Encoding": "gzip"}, headers_only=True)
        self.assertEqual(response.status, 415)
        response.read()
        self.assertEqual(self.requests, [])

    def test_gateway_rejections_and_redirects_are_not_retried_or_disclosed(self):
        for mode, expected in (("deny", 502), ("redirect", 502), ("rate-limit", 429)):
            with self.subTest(mode=mode):
                before = len(self.requests)
                response = self.request(self.payload(metadata={"mode": mode}))
                self.assertEqual(response.status, expected)
                raw = response.read()
                self.assertNotIn(b"access-secret", raw)
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1][0], "/v1/chat/completions")

    def test_incomplete_stream_emits_an_explicit_error_not_a_success_marker(self):
        response = self.request(self.payload(stream=True, metadata={"mode": "incomplete"}))
        raw = response.read()
        self.assertIn(b"event: error", raw)
        self.assertIn(b"without a completion marker", raw)
        self.assertNotIn(b"[DONE]", raw)
        self.assertTrue(any("incomplete" in message for message in self.logs))

    def test_disabling_blocks_new_requests_without_accessing_the_vault_or_gateway(self):
        self.connections.disable(self.connection.id)
        with patch.object(self.manager, "token", side_effect=AssertionError):
            response = self.request(headers_only=True)
        self.assertEqual(response.status, 503)
        self.assertIn("disabled", json.loads(response.read())["error"]["message"])
        self.assertEqual(self.requests, [])

    def test_readiness_and_model_discovery_are_authenticated_and_do_not_make_model_requests(self):
        for route in ("health", "models"):
            response = self.request(method="GET", path=self.connection.base_path + "/" + route)
            self.assertEqual(response.status, 200)
            self.assertNotIn(self.key, response.read().decode())
        self.assertEqual(self.requests, [])
        with patch("llm_credential_mapping.copilot.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.copilot.NativeKeyring", return_value=self.vault):
            ready = check(self.profiles)
        self.assertEqual(ready["connection"], self.connection.id)
        self.assertEqual(ready["customer"], "customer:A")
        self.assertEqual(ready["state"], "ready")

    def test_eight_request_slots_are_bounded_and_busy_is_not_success(self):
        for _ in range(8):
            self.assertTrue(self.bridge.slots.acquire(blocking=False))
        try:
            response = self.request()
            self.assertEqual(response.status, 503)
            self.assertIn("busy", json.loads(response.read())["error"]["message"])
        finally:
            for _ in range(8):
                self.bridge.slots.release()
        self.assertEqual(self.requests, [])

    def test_standard_node_fetch_can_read_openai_sse_through_the_bridge(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is not installed.")
        script = (
            "const [url,key] = process.argv.slice(1);"
            "const response = await fetch(url,{method:'POST',headers:{"
            "'Authorization':'Bearer '+key,'Content-Type':'application/json'},"
            "body:JSON.stringify({model:'model-alias',messages:[{role:'user',content:'fixture'}],stream:true})});"
            "if(!response.ok)throw new Error('HTTP '+response.status);"
            "const text = await response.text();"
            "if(!text.includes('data: [DONE]')||!text.includes('tool_calls'))throw new Error('Invalid SSE');"
            "console.log('OpenAI SSE consumed');"
        )
        result = subprocess.run([
            "node", "--input-type=module", "-e", script,
            f"http://127.0.0.1:{self.connection.port}{self.connection.base_path}/chat/completions", self.key,
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OpenAI SSE consumed", result.stdout)

    def test_foreground_lifecycle_observes_disabling_and_stops_without_a_background_service(self):
        production_connection = replace(self.connection, port=self.free_port())
        self.connections.put(production_connection, self.vault)
        ready = threading.Event()
        errors = []

        def notify(message):
            self.logs.append(message)
            if message.startswith("Serving "):
                ready.set()

        def run():
            try:
                serve(self.profiles, production_connection.id, notify)
            except CredentialError as error:
                errors.append(str(error))

        with patch("llm_credential_mapping.copilot.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.copilot.NativeKeyring", return_value=self.vault):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(ready.wait(5), errors)
            self.assertEqual(check(self.profiles, production_connection.id)["state"], "ready")
            self.connections.disable(production_connection.id)
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_stream_waits_for_the_complete_done_event_across_network_chunks(self):
        response = self.request(self.payload(stream=True, metadata={"mode": "split-completion"}))
        self.assertTrue(self.completion_sent.wait(5))
        for _ in range(10):
            line = response.readline()
            self.assertNotEqual(line, b"", "The stream closed before its completion event.")
            if b"[DONE]" in line:
                break
        else:
            self.fail("No completion event received.")
        finished = threading.Event()
        result = []

        def read_final_separator():
            result.append(response.readline())
            finished.set()

        worker = threading.Thread(target=read_final_separator, daemon=True)
        worker.start()
        try:
            self.assertFalse(finished.wait(0.2), "Bridge closed before forwarding the SSE event separator.")
            self.release_stream.set()
            self.assertTrue(finished.wait(5))
        finally:
            self.release_stream.set()
            worker.join(5)
        self.assertEqual(result, [b"\r\n"])
        self.assertEqual(response.read(), b"")

    def test_eight_mib_request_boundary_is_inclusive_and_next_byte_is_rejected(self):
        body = json.dumps(self.payload()).encode()
        body += b" " * (8_388_608 - len(body))
        response = self.request(raw=body)
        self.assertEqual(response.status, 200)
        response.read()
        response = self.request(headers={"Content-Length": "8388609"}, headers_only=True)
        self.assertEqual(response.status, 413)
        response.read()
        self.assertEqual(len(self.requests), 1)

    def test_non_ascii_or_unbounded_content_lengths_and_duplicate_headers_are_rejected(self):
        for length, expected in (("\xb2", 411), ("9" * 5000, 413)):
            response = self.request(headers={"Content-Length": length}, headers_only=True)
            self.assertEqual(response.status, expected)
            response.read()
        for name, value, expected in (
            ("Authorization", "Bearer " + self.key, 401), ("Host", "localhost", 403), ("Content-Length", "2", 411),
        ):
            client = http.client.HTTPConnection("127.0.0.1", self.connection.port, timeout=5)
            self.addCleanup(client.close)
            body = json.dumps(self.payload()).encode()
            client.putrequest("POST", self.connection.base_path + "/chat/completions")
            client.putheader("Authorization", "Bearer " + self.key)
            client.putheader("Content-Type", "application/json")
            client.putheader("Content-Length", str(len(body)))
            client.putheader(name, value)
            client.endheaders()
            response = client.getresponse()
            self.assertEqual(response.status, expected)
            response.read()
        self.assertEqual(self.requests, [])

    def test_explicit_compatible_output_limits_are_kept_without_injecting_another_parameter(self):
        for field in ("max_tokens", "max_completion_tokens"):
            response = self.request(self.payload(**{field: 2048}))
            self.assertEqual(response.status, 200)
            response.read()
            forwarded = self.requests[-1][2]
            self.assertEqual(forwarded[field], 2048)
            self.assertEqual(set(forwarded) & {"max_tokens", "max_completion_tokens"}, {field})

    def test_runtime_profile_edits_and_stale_model_routes_fail_closed(self):
        response = self.request(path="/llm-credentials/" + "a" * 64 + "/v1/chat/completions", headers_only=True)
        self.assertEqual(response.status, 404)
        response.read()
        self.profiles.path.write_text(json.dumps({
            "version": 1, "active": "unit", "profiles": {
                "unit": {"gateway": "https://changed.example", "team_id": "team", "allow_http": False},
            },
        }))
        response = self.request(headers_only=True)
        self.assertEqual(response.status, 503)
        self.assertIn("profile changed", json.loads(response.read())["error"]["message"])
        self.assertEqual(self.requests, [])

    def test_disabling_between_startup_and_loop_validation_is_a_normal_stop(self):
        connection = replace(self.connection, port=self.free_port())
        self.connections.put(connection, self.vault)
        original = CopilotBridge.require_enabled

        def disable_before_validation(server):
            server.store.disable(server.connection_info.id)
            original(server)

        with patch("llm_credential_mapping.copilot.current_repository", return_value=self.repository), \
                patch("llm_credential_mapping.copilot.NativeKeyring", return_value=self.vault), \
                patch.object(CopilotBridge, "require_enabled", disable_before_validation):
            serve(self.profiles, connection.id, self.logs.append)
        self.assertFalse(self.connections.select(connection.id).enabled)
        self.assertTrue(any("foreground bridge stopped" in message for message in self.logs))
