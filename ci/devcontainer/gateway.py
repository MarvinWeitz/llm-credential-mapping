"""A fake LiteLLM gateway for the dev container test: native sign-in, model list and streamed answers.

Run on the CI host; the dev container shares the host network, so both reach it at 127.0.0.1:PORT.
The sign-in issues a refresh token that must never appear inside the container. Model requests are
recorded with their headers; GET /__calls returns them.

    python ci/devcontainer/gateway.py PORT
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

ACCESS_TOKEN = "devcontainer-access-token"
REFRESH_TOKEN = "devcontainer-refresh-secret"


def sse(events) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int):
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.calls = []
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: Gateway

    def log_message(self, *args):
        pass

    def send(self, body, status=200, content_type="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        origin = self.server.origin
        path = urlsplit(self.path)
        if path.path == "/.well-known/litellm-cli-auth":
            self.send({
                "contract_version": 1, "issuer": origin, "resource": origin,
                "authorization_endpoint": origin + "/authorize", "registration_endpoint": origin + "/register",
                "token_endpoint": origin + "/token", "revocation_endpoint": origin + "/revoke",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
                "revocation_endpoint_auth_methods_supported": ["none"],
            })
        elif path.path == "/authorize":
            # Consent is automatic: send the browser straight back to the CLI's loopback callback.
            query = {key: values[0] for key, values in parse_qs(path.query).items()}
            self.send_response(302)
            self.send_header("Location", query["redirect_uri"] + "?" + urlencode({"code": "devcontainer-code", "state": query["state"]}))
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path.path.startswith("/v1/models"):
            self.send({"object": "list", "data": [{"id": "alias", "object": "model"}]})
        elif path.path == "/__calls":
            with self.server.lock:
                self.send(self.server.calls)
        else:
            self.send({"error": "not-found"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        path = urlsplit(self.path).path
        if path == "/register":
            body = json.loads(raw)
            self.send({"client_id": "devcontainer-client", "token_endpoint_auth_method": "none",
                       "redirect_uris": body["redirect_uris"]})
            return
        if path == "/token":
            self.send({"token_type": "Bearer", "access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN,
                       "expires_in": 3600, "user_id": "employee", "team_id": "team-a"})
            return
        if path == "/revoke":
            self.send({})
            return
        body = json.loads(raw or b"{}")
        with self.server.lock:
            self.server.calls.append({"path": path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if path.endswith("/count_tokens"):
            self.send({"input_tokens": 10})
        elif path.endswith("/messages"):
            self.messages(body)
        elif path.endswith("/chat/completions"):
            chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "alias"}
            events = [
                {**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "FIXTURE_OK"}, "finish_reason": None}]},
                {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
            ]
            data = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
            self.send(data.encode(), content_type="text/event-stream")
        else:  # Responses API (Codex)
            self.send(sse([
                ("response.created", {"type": "response.created", "response": {"id": "r"}}),
                ("response.output_item.done", {"type": "response.output_item.done", "item": {
                    "type": "message", "role": "assistant", "id": "m",
                    "content": [{"type": "output_text", "text": "FIXTURE_OK"}]}}),
                ("response.completed", {"type": "response.completed", "response": {"id": "r", "usage": {
                    "input_tokens": 5, "input_tokens_details": None, "output_tokens": 2,
                    "output_tokens_details": None, "total_tokens": 7}}}),
            ]), content_type="text/event-stream")

    def messages(self, body):
        message = {
            "id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture-model",
            "content": [{"type": "text", "text": "FIXTURE_OK"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        if not body.get("stream"):
            self.send(message)
            return
        self.send(sse([
            ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "FIXTURE_OK"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}),
            ("message_stop", {"type": "message_stop"}),
        ]), content_type="text/event-stream")


if __name__ == "__main__":
    Gateway(int(sys.argv[1])).serve_forever()
