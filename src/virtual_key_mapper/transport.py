import json
import unicodedata
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import CredentialError

# Appended to HTTP errors; callers that know login will not help drop it (the extensions offer "Sign in" on it).
HTTP_ERROR_ADVICE = " Check SSO configuration, membership and login."

REQUEST_TIMEOUT_SECONDS = 20
# How much of an OAuth error_description is shown; LiteLLM's are short sentences.
OAUTH_DESCRIPTION_CHARS = 200
# Registered OAuth error codes (RFC 6749, 6750, 7591, 8707 and OpenID); any other ``error`` value is not relayed.
OAUTH_ERRORS = frozenset({
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client", "unsupported_grant_type",
    "invalid_scope", "invalid_target", "access_denied", "unsupported_response_type", "server_error",
    "temporarily_unavailable", "invalid_token", "insufficient_scope", "invalid_redirect_uri",
    "invalid_client_metadata", "login_required",
})


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def printable(value: str, limit: int) -> str:
    """Untrusted gateway text made safe for a terminal: control and format characters removed, length capped."""
    text = "".join(char for char in value if unicodedata.category(char) not in ("Cc", "Cf"))
    return text if len(text) <= limit else text[:limit - 3] + "..."


class GatewayError(CredentialError):
    """A gateway request that failed in transit or with an HTTP error status.

    Bodies are never included, except an RFC 6749 error code (``oauth_error``) and its sanitized description.
    """

    def __init__(self, message: str, *, status: int | None = None, oauth_error: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.oauth_error = oauth_error

    @property
    def retryable(self) -> bool:
        """True when the same request may succeed later: no response, timeouts, throttling or server errors."""
        return self.status is None or self.status in (408, 429) or self.status >= 500


class JsonTransport:
    """Fetch JSON with verified TLS, bounded responses and no redirects or automatic retries."""

    def request(
        self, method: str, url: str, payload: dict | None = None, *, form=False, headers: dict[str, str] | None = None,
    ) -> dict:
        value = self._request(method, url, payload, form=form, headers=headers)
        if not isinstance(value, dict):
            raise CredentialError("Gateway returned an unexpected JSON structure.")
        return value

    def get_list(self, url: str, *, headers: dict[str, str]) -> list:
        """Read an authenticated JSON array without redirects; reject other response shapes."""
        value = self._request("GET", url, headers=headers)
        if not isinstance(value, list):
            raise CredentialError("Gateway returned an unexpected JSON list structure.")
        return value

    def _request(
        self, method: str, url: str, payload: dict | None = None, *,
        form=False, headers: dict[str, str] | None = None,
    ) -> dict | list:
        headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if payload is not None:
            if form:
                data = urlencode(payload).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with build_opener(NoRedirects()).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(1_048_577)
        except HTTPError as error:
            code = error.code
            try:
                oauth_error, detail = _oauth_error(error, [*(payload or {}).values(), *headers.values()])
            finally:
                error.close()
            if 300 <= code < 400:
                raise GatewayError(
                    "Gateway redirect refused; configure its final public HTTPS origin.", status=code,
                ) from None
            raise GatewayError(
                f"Gateway returned HTTP {code}{detail}.{HTTP_ERROR_ADVICE}",
                status=code, oauth_error=oauth_error,
            ) from None
        except (URLError, OSError, HTTPException):
            raise GatewayError("Gateway connection failed; check connectivity and trusted TLS certificates.") from None
        if len(raw) > 1_048_576:
            raise CredentialError("Gateway JSON response exceeded the size limit.")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise CredentialError("Gateway returned invalid JSON.") from None
        if not isinstance(value, (dict, list)):
            raise CredentialError("Gateway returned an unexpected JSON structure.")
        return value


def _oauth_error(response: HTTPError, sent: list) -> tuple[str | None, str]:
    """The registered OAuth ``error`` code of an error body and a message suffix; nothing for other bodies."""
    try:
        value = json.loads(response.read(65536))
    except (ValueError, UnicodeError, RecursionError, OSError, HTTPException):
        return None, ""
    code = value.get("error") if isinstance(value, dict) else None
    if not isinstance(code, str) or code not in OAUTH_ERRORS:
        return None, ""
    description = value.get("error_description")
    secrets = [word for item in sent if isinstance(item, str) for word in item.split() if len(word) >= 8]
    if not isinstance(description, str) or any(secret in description for secret in secrets):
        # Never relay a description that echoes a token, code or verifier from the request.
        return code, f" ({code})"
    description = printable(description, OAUTH_DESCRIPTION_CHARS).strip()
    return code, f" ({code}: {description})" if description else f" ({code})"
