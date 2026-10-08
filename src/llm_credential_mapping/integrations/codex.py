import json

from ..adapters import Api, IntegrationContext
from ..customers import Attribution
from .common import TOKEN_VARIABLE, attribution_headers, child_environment, header_variable, run

PROVIDER = "llm-credential-mapping"


class Codex:
    """Codex through the gateway's OpenAI Responses API, configured with -c overrides only."""

    name = "codex"

    def command(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        env = child_environment(context, attribution)
        base_url = context.gateway.endpoint(Api.OPENAI_RESPONSES)
        env["OPENAI_API_KEY"] = context.token
        env["OPENAI_BASE_URL"] = base_url
        settings = {
            "model_provider": PROVIDER,
            f"model_providers.{PROVIDER}.name": "llm-credentials",
            f"model_providers.{PROVIDER}.base_url": base_url,
            f"model_providers.{PROVIDER}.wire_api": "responses",
            f"model_providers.{PROVIDER}.env_key": TOKEN_VARIABLE,
            f"model_providers.{PROVIDER}.requires_openai_auth": False,
        }
        # Codex reads header values from variables named in its config, so values never reach argv.
        for name, value in attribution_headers(context, attribution).items():
            env[header_variable(name)] = value
            settings[f"model_providers.{PROVIDER}.env_http_headers.{name}"] = header_variable(name)
        command = ["codex"]
        for key, value in settings.items():
            command.extend(["-c", key + "=" + json.dumps(value)])
        if model:
            command.extend(["--model", model])
        return command + args, env

    def launch(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> int:
        return run(self.name, *self.command(context, attribution, args, model))
