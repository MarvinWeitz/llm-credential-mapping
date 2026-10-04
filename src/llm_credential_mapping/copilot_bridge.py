import hmac
import json
import math
import os
import re
import socket
import threading
from http.client import HTTPException, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .copilot import CopilotConnection, CopilotStore, connection_key, validate_connection
from .errors import CredentialError
from .profiles import Profile, ProfileStore
from .sessions import SessionManager
from .transport import NoRedirects


MAX_REQUEST_BYTES = 8_388_608
_CHAT_FIELDS = {
    "model", "messages", "stream", "stream_options", "temperature", "top_p", "max_tokens",
    "max_completion_tokens", "n", "stop", "tools", "tool_choice", "parallel_tool_calls",
    "response_format", "frequency_penalty", "presence_penalty", "logprobs", "top_logprobs",
    "logit_bias", "seed", "reasoning_effort", "thinking", "verbosity", "prediction",
    "audio", "modalities", "service_tier", "store", "metadata", "user",
}
_METADATA_RESERVED = {"tags", "team_id", "end_user_id", "user_id"}


def _reject_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON constant.")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number.")
    return result


class _ConnectionDisabled(CredentialError):
    pass


class CopilotBridge(ThreadingHTTPServer):
    """Authenticated IPv4 loopback Chat Completions relay for one immutable Copilot connection.

    Only the pinned model and employee grant are forwarded. Client credentials,
    routing overrides and customer tags cannot replace the bridge's identity.
    """

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(
        self, store: CopilotStore, connection: CopilotConnection, profile: Profile,
        manager: SessionManager, local_key: str, notify: Callable[[str], None], *, bind_port: int | None = None,
    ) -> None:
        self.store = store
        self.connection_info = connection
        self.profile = profile
        self.manager = manager
        self.local_key = local_key
        self.notify = notify
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", connection.port if bind_port is None else bind_port), _Handler)
        self.timeout = 1

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def require_enabled(self) -> None:
        current = self.store.select(self.connection_info.id)
        if not current.enabled:
            raise _ConnectionDisabled("The Copilot connection was disabled; choose a normal Copilot model.")
        if current != self.connection_info:
            raise CredentialError("The saved Copilot context changed; restart the bridge.")
        if self.store.profiles.get(self.connection_info.profile).account != self.connection_info.account:
            raise CredentialError("The pinned authentication profile changed; rerun setup and restart the bridge.")


class _Handler(BaseHTTPRequestHandler):
    server: CopilotBridge
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_request(self, code="-", size="-") -> None:
        self.server.notify(f"Copilot bridge HTTP {code}; connection {self.server.connection_info.id[:12]}.")

    def log_message(self, format, *args) -> None:
        self.server.notify("Copilot bridge HTTP protocol diagnostic; request details withheld.")

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            self.server.notify("Copilot client disconnected before its response.")

    def _error(self, code: int, message: str, kind: str = "bridge_error") -> None:
        self._json(code, {"error": {"message": message, "type": "llm_credentials_error", "code": kind}})

    def _authorised(self) -> bool:
        host = self.headers.get("Host", "")
        if (
            len(self.headers.get_all("Host", [])) != 1
            or not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", host, re.IGNORECASE)
            or self.headers.get("Origin") is not None
        ):
            self._error(403, "Only authenticated loopback clients without a browser Origin are accepted.")
            return False
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1 or not values[0].lower().startswith("bearer ") or not hmac.compare_digest(
            values[0][7:].encode("utf-8"), self.server.local_key.encode("utf-8"),
        ):
            self._error(401, "Enter the local key from 'copilot key' in this provider's API key field.", "local_key_required")
            return False
        try:
            self.server.require_enabled()
        except CredentialError as error:
            self._error(503, str(error), "connection_unavailable")
            return False
        return True

    def do_OPTIONS(self) -> None:
        self._error(403, "Browser access and cross-origin requests are not supported.")

    def do_GET(self) -> None:
        if not self._authorised():
            return
        info = self.server.connection_info
        if self.path == info.base_path + "/health":
            try:
                self.server.manager.token(self.server.profile, expected_user=info.user_id)
            except CredentialError as error:
                self._error(503, str(error), "employee_credentials_unavailable")
                return
            self._json(200, {
                "state": "ready", "connection": info.id, "profile": info.profile,
                "repository": info.repository, "customer": info.customer, "model": info.model,
            })
        elif self.path == info.base_path + "/models":
            self._json(200, {"object": "list", "data": [{
                "id": info.model, "object": "model", "owned_by": "llm-credential-mapping",
            }]})
        else:
            self._error(404, "This route does not belong to the pinned connection.")

    def _payload(self) -> dict | None:
        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
            or not re.fullmatch(r"[0-9]+", lengths[0])
        ):
            self._error(411, "A single Content-Length is required; chunked request bodies are not supported.")
            return None
        if len(lengths[0]) > 10:
            self._error(413, "The request must be nonempty and no larger than 8 MiB.")
            return None
        length = int(lengths[0])
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._error(413, "The request must be nonempty and no larger than 8 MiB.")
            return None
        if self.headers.get_content_type() != "application/json" or self.headers.get("Content-Encoding") is not None:
            self._error(415, "Send uncompressed application/json.")
            return None
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                self._error(400, "Incomplete request body.")
                return None
            payload = json.loads(raw, parse_constant=_reject_constant, parse_float=_finite_float)
        except (ValueError, UnicodeError, RecursionError):
            self._error(400, "Invalid JSON request body.")
            return None
        info = self.server.connection_info
        if (
            not isinstance(payload, dict) or set(payload) - _CHAT_FIELDS
            or payload.get("model") != info.model
            or not isinstance(payload.get("messages"), list) or not payload["messages"]
            or type(payload.get("stream", False)) is not bool
            or not isinstance(payload.get("metadata", {}), dict)
        ):
            self._error(400, "Use the pinned model, a nonempty messages array and supported Chat Completions fields.")
            return None
        for name in ("max_tokens", "max_completion_tokens"):
            if name in payload and (
                type(payload[name]) is not int or not 1 <= payload[name] <= info.max_output_tokens
            ):
                self._error(400, "Requested output tokens exceed the configured model limit.")
                return None
        if "max_tokens" not in payload and "max_completion_tokens" not in payload:
            payload["max_completion_tokens"] = info.max_output_tokens
        payload["user"] = info.user_id
        if "metadata" in payload:
            payload["metadata"] = {
                name: value for name, value in payload["metadata"].items()
                if name not in _METADATA_RESERVED and not name.startswith(("user_api_key", "litellm"))
            }
        return payload

    def do_POST(self) -> None:
        if not self._authorised():
            return
        if self.path != self.server.connection_info.base_path + "/chat/completions":
            self._error(404, "This route does not belong to the pinned connection.")
            return
        try:
            payload = self._payload()
        except (OSError, HTTPException):
            self._error(400, "Cannot read the request body.")
            return
        if payload is None:
            return
        if not self.server.slots.acquire(blocking=False):
            self._error(503, "The local bridge is busy; retry shortly.")
            return
        try:
            self._forward(payload)
        finally:
            self.server.slots.release()

    def _forward(self, payload: dict) -> None:
        info = self.server.connection_info
        try:
            self.server.require_enabled()
            token = self.server.manager.token(self.server.profile, expected_user=info.user_id)
        except CredentialError as error:
            self._error(503, str(error), "employee_credentials_unavailable")
            return
        request = Request(
            self.server.profile.gateway + "/v1/chat/completions",
            data=json.dumps(payload, allow_nan=False).encode(),
            headers={
                "Authorization": "Bearer " + token, "x-litellm-tags": info.customer,
                "Content-Type": "application/json", "Accept-Encoding": "identity",
                "User-Agent": "llm-credential-mapping-copilot",
            }, method="POST",
        )
        try:
            response = build_opener(NoRedirects()).open(request, timeout=120)
        except HTTPError as error:
            code = error.code
            error.close()
            self._error(
                429 if code == 429 else 502,
                f"LiteLLM returned HTTP {code}; check the CLI login, gateway permissions and model availability.",
                "gateway_rejected_request",
            )
            return
        except (URLError, OSError, HTTPException):
            self._error(502, "Cannot connect to LiteLLM; check connectivity and trusted TLS certificates.")
            return
        with response:
            if not isinstance(response, HTTPResponse):
                self._error(502, "The gateway returned an unsupported HTTP response.")
                return
            streaming = payload.get("stream", False)
            expected_type = "text/event-stream" if streaming else "application/json"
            if (
                response.status != 200 or response.headers.get_content_type() != expected_type
                or response.getheader("Content-Encoding") not in (None, "identity")
            ):
                self._error(502, "LiteLLM returned an unexpected status, content type or encoding.")
                return
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", expected_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            pending = b""
            done = False
            event_data_count = 0
            event_is_done = False
            try:
                while chunk := response.read1(16_384):
                    if streaming:
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            line = line.removesuffix(b"\r")
                            if not line:
                                done = done or event_is_done
                                event_data_count = 0
                                event_is_done = False
                            elif line.startswith(b"data:"):
                                event_is_done = event_data_count == 0 and line[5:].removeprefix(b" ") == b"[DONE]"
                                event_data_count += 1
                        if len(pending) > 1_048_576:
                            raise OSError("Oversized SSE event.")
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if streaming and done:
                        break
                if streaming and not done:
                    self._stream_error("LiteLLM closed the stream without a completion marker.")
            except (BrokenPipeError, ConnectionResetError):
                self.server.notify("Copilot client cancelled or disconnected; upstream response closed.")
            except (OSError, HTTPException):
                if streaming:
                    self._stream_error("The LiteLLM stream failed; retry only after checking the request outcome.")
                else:
                    self.server.notify("Copilot bridge response transfer failed; the client must retry explicitly.")

    def _stream_error(self, message: str) -> None:
        self.server.notify("Copilot bridge: incomplete or failed upstream stream.")
        raw = json.dumps({"error": {"message": message, "type": "llm_credentials_error"}}).encode()
        try:
            self.wfile.write(b"\nevent: error\ndata: " + raw + b"\n\n")
            self.wfile.flush()
        except OSError:
            self.server.notify("Copilot client disconnected during stream error reporting.")


def check(store: ProfileStore, identifier: str | None = None) -> dict:
    """Verify the selected foreground bridge and pinned employee; return nonsecret readiness details or raise CredentialError."""
    info = CopilotStore(store).select(identifier)
    _, manager = validate_connection(store, info)
    key = connection_key(info, manager.secrets)
    request = Request(
        f"http://127.0.0.1:{info.port}{info.base_path}/health",
        headers={"Authorization": "Bearer " + key},
    )
    try:
        with build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=10) as response:
            raw = response.read(8193)
    except HTTPError as error:
        code = error.code
        error.close()
        raise CredentialError(f"The local Copilot bridge rejected the check (HTTP {code}); verify its connection.") from None
    except (URLError, OSError, HTTPException):
        raise CredentialError("No reachable Copilot bridge; run 'copilot serve' in its extension-host terminal.") from None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CredentialError("The local Copilot bridge returned invalid readiness data.") from None
    expected = {
        "state": "ready", "connection": info.id, "profile": info.profile,
        "repository": info.repository, "customer": info.customer, "model": info.model,
    }
    if len(raw) > 8192 or value != expected:
        raise CredentialError("The local Copilot endpoint does not match the selected pinned connection.")
    return value


def serve(store: ProfileStore, identifier: str | None, notify: Callable[[str], None]) -> None:
    """Run one foreground loopback bridge until Ctrl+C or CLI disabling; require its pinned checkout and OS-vault credentials."""
    connections = CopilotStore(store)
    info = connections.select(identifier)
    profile, manager = validate_connection(store, info)
    key = connection_key(info, manager.secrets)
    try:
        server = CopilotBridge(connections, info, profile, manager, key, notify)
    except OSError:
        raise CredentialError("Cannot bind the Copilot loopback port; check for another service or bridge using it.") from None
    notify(f"Serving {info.group_name} on http://127.0.0.1:{info.port}{info.base_path}. Keep this terminal open.")
    try:
        while True:
            server.require_enabled()
            server.handle_request()
    except _ConnectionDisabled:
        notify("Copilot connection disabled; foreground bridge stopped.")
    except KeyboardInterrupt:
        notify("Copilot bridge stopped. Pick a normal Copilot model, or rerun serve to reconnect.")
    finally:
        server.server_close()
