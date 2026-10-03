import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import CredentialError


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonTransport:
    """Fetch JSON with verified TLS, bounded responses and no redirects or automatic retries."""

    def request(self, method: str, url: str, payload: dict | None = None, *, form=False) -> dict:
        value = self._request(method, url, payload, form=form)
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
            with build_opener(NoRedirects()).open(request, timeout=20) as response:
                raw = response.read(1_048_577)
        except HTTPError as error:
            code = error.code
            error.close()
            if 300 <= code < 400:
                raise CredentialError("Gateway redirect refused; configure its final public HTTPS origin.") from None
            raise CredentialError(
                f"Gateway returned HTTP {code}. Check SSO configuration, membership and login."
            ) from None
        except (URLError, OSError, HTTPException):
            raise CredentialError("Gateway connection failed; check connectivity and trusted TLS certificates.") from None
        if len(raw) > 1_048_576:
            raise CredentialError("Gateway JSON response exceeded the size limit.")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise CredentialError("Gateway returned invalid JSON.") from None
        if not isinstance(value, (dict, list)):
            raise CredentialError("Gateway returned an unexpected JSON structure.")
        return value
