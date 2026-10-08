import os
import re

from ..adapters import Api, IntegrationContext
from ..customers import Attribution
from ..errors import CredentialError
from .common import CUSTOMER_VARIABLE, attribution_headers, child_environment, run


def _claude_headers(existing: str, headers: dict[str, str]) -> str:
    """Merge attribution headers into ANTHROPIC_CUSTOM_HEADERS, replacing inherited ones and keeping unrelated ones."""
    replaced = {name.lower() for name in headers}
    lines = []
    for line in existing.splitlines():
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise CredentialError("Existing ANTHROPIC_CUSTOM_HEADERS contains an invalid header.")
        if name.lower() in ("authorization", "x-api-key", "host"):
            raise CredentialError("Remove authentication/routing overrides from ANTHROPIC_CUSTOM_HEADERS before launch.")
        if any(not 32 <= ord(char) <= 126 for char in value):
            raise CredentialError("Existing ANTHROPIC_CUSTOM_HEADERS contains an invalid header value.")
        if name.lower() not in replaced:
            lines.append(line)
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    return "\n".join(lines)


def claude_environment(
    base_url: str, headers: dict[str, str], model: str | None = None, customer: str | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Copy the environment and configure Claude routing and attribution; no credential is fetched or added."""
    env = (os.environ if environment is None else environment).copy()
    env.pop("ANTHROPIC_API_KEY", None)
    for flag in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        env.pop(flag, None)
    env["ANTHROPIC_BASE_URL"] = base_url
    if customer is not None:
        env[CUSTOMER_VARIABLE] = customer
    if headers:
        env["ANTHROPIC_CUSTOM_HEADERS"] = _claude_headers(env.get("ANTHROPIC_CUSTOM_HEADERS", ""), headers)
    if model:
        env["ANTHROPIC_MODEL"] = model
    return env


class Claude:
    """Claude Code through the gateway's Anthropic Messages API."""

    name = "claude"

    def command(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        env = claude_environment(
            context.gateway.endpoint(Api.ANTHROPIC_MESSAGES), attribution_headers(context, attribution), model,
            attribution.customer if attribution is not None else None, child_environment(context, attribution),
        )
        env["ANTHROPIC_AUTH_TOKEN"] = context.token
        return ["claude", *args], env

    def launch(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> int:
        return run(self.name, *self.command(context, attribution, args, model))
