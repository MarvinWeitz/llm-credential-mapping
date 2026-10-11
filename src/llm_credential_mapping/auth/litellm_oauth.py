import base64
import hashlib
import hmac
import logging
import math
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from ..errors import CredentialError
from ..profiles import Profile, gateway_origin
from ..transport import JsonTransport


def team_id(profile: Profile) -> str:
    """The LiteLLM team chosen at browser consent; validated by the litellm-sso auth kind."""
    return profile.auth["team_id"]


def required_text(data: dict, name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value or len(value) > 65536:
        raise CredentialError(f"Gateway response is missing a valid {name}.")
    if any(ord(char) < 32 for char in value):
        raise CredentialError(f"Gateway returned an invalid {name}.")
    return value


@dataclass(frozen=True)
class Discovery:
    """Validated version-1 native LiteLLM OAuth endpoints on the configured gateway origin."""

    issuer: str
    authorization_endpoint: str
    registration_endpoint: str
    token_endpoint: str
    revocation_endpoint: str
    resource: str

    @classmethod
    def parse(cls, data: dict, profile: Profile) -> "Discovery":
        if type(data.get("contract_version")) is not int or data["contract_version"] != 1:
            raise CredentialError("Gateway does not expose the supported LiteLLM native login contract v1.")
        for key, required in (
            ("code_challenge_methods_supported", {"S256"}),
            ("response_types_supported", {"code"}),
            ("grant_types_supported", {"authorization_code", "refresh_token"}),
            ("token_endpoint_auth_methods_supported", {"none"}),
            ("revocation_endpoint_auth_methods_supported", {"none"}),
        ):
            values = data.get(key)
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise CredentialError(f"Gateway discovery has invalid {key}.")
            if not required.issubset(values):
                raise CredentialError(f"Gateway does not support required {key}.")
        endpoints = {}
        for key in (
            "issuer", "authorization_endpoint", "registration_endpoint",
            "token_endpoint", "revocation_endpoint", "resource",
        ):
            value = required_text(data, key)
            _require_gateway_url(value, profile)
            if key in ("issuer", "resource") and gateway_origin(value, profile.allow_http) != profile.gateway:
                raise CredentialError("Gateway issuer/resource does not match the configured origin.")
            endpoints[key] = value
        return cls(**endpoints)


class IdentityChanged(CredentialError):
    """A renewal returned a grant for another employee or team; the session must not be used any more."""


_SESSION_FIELDS = (
    "gateway", "team_id", "user_id", "client_id", "access_token", "refresh_token",
    "issued_at", "expires_at", "token_endpoint", "revocation_endpoint", "resource",
    "needs_login", "blocked", "retry_after", "refresh_failures",
)
_ENDPOINT_FIELDS = ("token_endpoint", "revocation_endpoint", "resource")


@dataclass(frozen=True)
class Session:
    """A single employee/team OAuth grant and the endpoints that renew or revoke it; credentials are excluded from repr.

    Renewal state: ``needs_login`` means the gateway refused the refresh grant, so no further renewal is
    attempted while the access token is still released until it nearly expires. ``blocked`` means a renewal
    changed identity; nothing is released. ``retry_after``/``refresh_failures`` back off temporary failures.
    """

    gateway: str
    team_id: str
    user_id: str
    client_id: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    issued_at: float
    expires_at: float
    token_endpoint: str
    revocation_endpoint: str
    resource: str
    needs_login: bool = False
    blocked: bool = False
    retry_after: float | None = None
    refresh_failures: int = 0

    @classmethod
    def from_response(cls, data: dict, profile: Profile, client_id: str, endpoints: "Discovery | Session") -> "Session":
        if required_text(data, "token_type").lower() != "bearer":
            raise CredentialError("Gateway returned an unsupported token type.")
        expiry = data.get("expires_in")
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
            raise CredentialError("Gateway returned an invalid token expiry.")
        now = time.time()
        return cls(
            profile.gateway,
            required_text(data, "team_id"),
            required_text(data, "user_id"),
            client_id,
            required_text(data, "access_token"),
            required_text(data, "refresh_token"),
            now,
            now + expiry,
            endpoints.token_endpoint,
            endpoints.revocation_endpoint,
            endpoints.resource,
        )

    def check_binding(self, profile: Profile) -> None:
        """Require this grant to belong to the profile's gateway and team, with endpoints on that gateway."""
        if self.gateway != profile.gateway or self.team_id != team_id(profile):
            raise CredentialError(
                f"Credential team does not match profile '{profile.name}'. "
                "Log in again and choose its configured LiteLLM team."
            )
        for name in _ENDPOINT_FIELDS:
            _require_gateway_url(getattr(self, name), profile)

    @property
    def lifetime(self) -> float:
        return self.expires_at - self.issued_at

    def to_dict(self) -> dict:
        return {"version": 1, **self.__dict__}

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        if set(data) != {"version", *_SESSION_FIELDS} or type(data["version"]) is not int or data["version"] != 1:
            raise CredentialError("Unsupported stored session; log in again.")
        for name in _SESSION_FIELDS[:-4]:
            if name not in ("issued_at", "expires_at", "needs_login"):
                required_text(data, name)

        def number(value) -> bool:
            return type(value) in (float, int) and math.isfinite(value)

        if (
            not all(number(data[name]) for name in ("issued_at", "expires_at"))
            or data["issued_at"] > data["expires_at"]
            or any(type(data[name]) is not bool for name in ("needs_login", "blocked"))
            or (data["retry_after"] is not None and not number(data["retry_after"]))
            or type(data["refresh_failures"]) is not int or data["refresh_failures"] < 0
        ):
            raise CredentialError("Invalid stored session expiry/state; log in again.")
        return cls(**{name: data[name] for name in _SESSION_FIELDS})


def _require_gateway_url(value: str, profile: Profile) -> None:
    try:
        parsed = urlsplit(value)
        origin = gateway_origin(f"{parsed.scheme}://{parsed.netloc}", profile.allow_http)
    except (ValueError, CredentialError):
        raise CredentialError("Gateway endpoint is invalid.") from None
    if origin != profile.gateway or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CredentialError("Gateway endpoint is outside the trusted gateway origin.")


class LoopbackCallback:
    """Receive one state-verified OAuth response on an ephemeral IPv4 loopback listener."""

    def __init__(self, state: str, notify: Callable[[str], None] | None = None) -> None:
        self.result: str | CredentialError | None = None
        owner = self

        class CallbackServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address) -> None:
                message = "Warning: callback connection failed; retry login if it does not complete."
                if notify is not None:
                    notify(message)
                else:
                    logging.getLogger(__name__).warning(message)

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                self.request.settimeout(3)
                super().setup()

            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                states = query.get("state", [])
                valid = (
                    self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                    and parsed.path == "/callback"
                    and len(states) == 1
                    and hmac.compare_digest(states[0].encode(), state.encode())
                )
                if not valid:
                    self.send_error(400, "Invalid callback")
                    return
                if "error" in query:
                    owner.result = CredentialError("Authorization was denied in the browser.")
                elif len(query.get("code", [])) == 1 and query["code"][0]:
                    owner.result = query["code"][0]
                else:
                    self.send_error(400, "Missing authorization code")
                    return
                message = b"Login response received. Return to your terminal."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                self.wfile.write(message)

        try:
            self.server = CallbackServer(("127.0.0.1", 0), Handler)
        except OSError:
            raise CredentialError("Cannot open the local login callback listener.") from None
        self.server.timeout = 0.5
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_port}/callback"

    def wait(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while self.result is None and time.monotonic() < deadline:
            self.server.handle_request()
        if isinstance(self.result, CredentialError):
            raise self.result
        if self.result is None:
            raise CredentialError("Login timed out; run login again.")
        return self.result

    def close(self) -> None:
        self.server.server_close()


class LiteLLMClient:
    """Consume native LiteLLM SSO; no admin keys, legacy sk-key mapping or team override."""

    def __init__(self, profile: Profile, transport: JsonTransport | None = None) -> None:
        self.profile = profile
        self.transport = transport or JsonTransport()

    def discover(self) -> Discovery:
        data = self.transport.request(
            "GET", self.profile.gateway + "/.well-known/litellm-cli-auth"
        )
        return Discovery.parse(data, self.profile)

    def login(
        self,
        notify: Callable[[str], None],
        *,
        no_browser: bool = False,
        timeout: float = 180,
    ) -> Session:
        """Open SSO and return an employee/team grant; browser consent still selects the team."""
        discovery = self.discover()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        callback = LoopbackCallback(state, notify)
        try:
            registration = self.transport.request(
                "POST",
                discovery.registration_endpoint,
                {
                    "client_name": "LLM Credential Mapping",
                    "redirect_uris": [callback.redirect_uri],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
            client_id = required_text(registration, "client_id")
            if (
                registration.get("token_endpoint_auth_method") != "none"
                or registration.get("redirect_uris") != [callback.redirect_uri]
            ):
                raise CredentialError("Gateway registered an unexpected OAuth client.")
            url = discovery.authorization_endpoint + "?" + urlencode({
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": callback.redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": discovery.resource,
            })
            notify(
                f"Choose the LiteLLM team for '{self.profile.name}' "
                f"(expected ID: {team_id(self.profile)}) in browser consent."
            )
            if no_browser:
                notify(url)
            elif not webbrowser.open(url):
                raise CredentialError("Cannot open a browser; rerun login with --no-browser.")
            code = callback.wait(timeout)
            response = self.transport.request(
                "POST",
                discovery.token_endpoint,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": callback.redirect_uri,
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": discovery.resource,
                },
                form=True,
            )
            return Session.from_response(response, self.profile, client_id, discovery)
        finally:
            callback.close()

    def refresh(self, session: Session) -> Session:
        """Rotate the refresh grant at the endpoint stored at login; never rediscover or follow redirects.

        Raise GatewayError for transport/HTTP failures (see GatewayError.retryable), CredentialError
        for an unusable response and IdentityChanged for a changed employee/team, after revoking that
        unexpected grant.
        """
        response = self.transport.request(
            "POST",
            session.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": session.refresh_token,
                "client_id": session.client_id,
                "resource": session.resource,
            },
            form=True,
        )
        renewed = Session.from_response(response, self.profile, session.client_id, session)
        try:
            renewed.check_binding(self.profile)
            if renewed.user_id != session.user_id:
                raise CredentialError("Renewed credential changed employee identity; log in again.")
        except CredentialError as error:
            try:
                self.revoke(renewed)
            except CredentialError:
                raise IdentityChanged(
                    f"{error} Unexpected refresh grant could not be revoked; contact the gateway administrator."
                ) from None
            raise IdentityChanged(str(error)) from None
        return renewed

    def revoke(self, session: Session) -> None:
        self.transport.request(
            "POST",
            session.revocation_endpoint,
            {"token": session.refresh_token, "client_id": session.client_id},
            form=True,
        )
