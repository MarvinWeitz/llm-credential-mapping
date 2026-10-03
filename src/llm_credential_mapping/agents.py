import json
import os
import shutil
import subprocess

from .errors import CredentialError
from .profiles import Profile

TOKEN_VARIABLE = "LLM_CREDENTIAL_MAPPING_TOKEN"


def agent_command(
    agent: str, profile: Profile, token: str, args: list[str], model: str | None = None
) -> tuple[list[str], dict[str, str]]:
    """Build a child-only gateway configuration; tokens never appear in command arguments."""
    env = os.environ.copy()
    env[TOKEN_VARIABLE] = token
    if agent == "claude":
        env.pop("ANTHROPIC_API_KEY", None)
        for flag in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
            env.pop(flag, None)
        env["ANTHROPIC_BASE_URL"] = profile.gateway
        env["ANTHROPIC_AUTH_TOKEN"] = token
        if model:
            env["ANTHROPIC_MODEL"] = model
        command = ["claude"]
    elif agent == "codex":
        env["OPENAI_API_KEY"] = token
        env["OPENAI_BASE_URL"] = profile.gateway + "/v1"
        provider = "llm-credential-mapping"
        settings = {
            "model_provider": provider,
            f"model_providers.{provider}.name": "LiteLLM project",
            f"model_providers.{provider}.base_url": profile.gateway + "/v1",
            f"model_providers.{provider}.wire_api": "responses",
            f"model_providers.{provider}.env_key": TOKEN_VARIABLE,
            f"model_providers.{provider}.requires_openai_auth": False,
        }
        command = ["codex"]
        for key, value in settings.items():
            command.extend(["-c", key + "=" + json.dumps(value)])
        if model:
            command.extend(["--model", model])
    elif agent == "opencode":
        if not model:
            raise CredentialError("OpenCode requires --model with an available LiteLLM model alias.")
        try:
            config = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        except ValueError:
            raise CredentialError("Existing OPENCODE_CONFIG_CONTENT is invalid JSON.") from None
        if not isinstance(config, dict) or not isinstance(config.get("provider", {}), dict):
            raise CredentialError("Existing OpenCode provider configuration is invalid.")
        provider = "llm-credential-mapping"
        config.setdefault("provider", {})[provider] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "LiteLLM project",
            "options": {
                "baseURL": profile.gateway + "/v1",
                "apiKey": "{env:" + TOKEN_VARIABLE + "}",
            },
            "models": {model: {"name": model}},
        }
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        command = ["opencode", "--model", provider + "/" + model]
    else:
        raise CredentialError("Unsupported agent; choose claude, codex or opencode.")
    return command + args, env


def launch(agent: str, profile: Profile, token: str, args: list[str], model: str | None) -> int:
    command, env = agent_command(agent, profile, token, args, model)
    executable = shutil.which(command[0])
    if executable is None:
        raise CredentialError(f"Install '{agent}' and make it available on PATH.")
    command[0] = executable
    try:
        return subprocess.run(command, env=env, check=False).returncode
    except OSError:
        raise CredentialError(f"Cannot start '{agent}'; check its installation.") from None
