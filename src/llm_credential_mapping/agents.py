import json
import os
import re
import shutil
import subprocess

from .errors import CredentialError
from .profiles import Profile
from .tags import TAG_HEADER, validate_tag

TOKEN_VARIABLE = "LLM_CREDENTIAL_MAPPING_TOKEN"
CUSTOMER_VARIABLE = "LLM_CREDENTIAL_MAPPING_CUSTOMER"


def _claude_headers(existing: str, customer: str) -> str:
    headers = []
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
        if name.lower() != TAG_HEADER:
            headers.append(line)
    headers.append(f"{TAG_HEADER}: {customer}")
    return "\n".join(headers)


def claude_environment(
    profile: Profile, model: str | None = None, customer: str | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Copy the environment and configure Claude routing/tags; no credential is fetched or added."""
    env = (os.environ if environment is None else environment).copy()
    env.pop("ANTHROPIC_API_KEY", None)
    for flag in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        env.pop(flag, None)
    env["ANTHROPIC_BASE_URL"] = profile.gateway
    if customer is not None:
        env[CUSTOMER_VARIABLE] = validate_tag(customer)
        env["ANTHROPIC_CUSTOM_HEADERS"] = _claude_headers(env.get("ANTHROPIC_CUSTOM_HEADERS", ""), customer)
    if model:
        env["ANTHROPIC_MODEL"] = model
    return env


def _opencode_command(args: list[str], major: int = 1) -> list[str]:
    if args and args[0] == "exec":
        raise CredentialError("OpenCode uses 'run', not Codex's 'exec': opencode -- run \"Your prompt\".")
    flag_args = args[:args.index("--")] if "--" in args else args
    if any(
        arg in ("--server", "--attach") or arg.startswith(("--server=", "--attach="))
        for arg in flag_args
    ):
        raise CredentialError("OpenCode sessions cannot attach to a shared server; it would bypass customer/credential isolation.")
    if major == 1:
        return ["opencode", *args]
    if major != 2:
        raise CredentialError("Supported OpenCode versions are v1 and v2; this version needs an updated adapter.")
    if any(arg.startswith("--standalone=") and arg != "--standalone=true" for arg in flag_args):
        raise CredentialError("The OpenCode v2 launcher cannot disable its private server.")
    if args and args[0] == "run":
        return ["opencode", "run", "--standalone", *args[1:]]
    if args and not args[0].startswith("-") and not os.path.isdir(args[0]):
        raise CredentialError(
            "OpenCode v2 launch mode supports interactive use or 'run'. "
            "Use the unwrapped OpenCode command for other subcommands."
        )
    return ["opencode", "--standalone", *args]


def _opencode_major(executable: str) -> int:
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise CredentialError("Cannot check OpenCode's version; verify 'opencode --version' works.") from None
    match = re.fullmatch(
        r"(?:opencode\s+)?v?(\d+)\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?",
        result.stdout.strip(),
        re.IGNORECASE,
    )
    if result.returncode != 0 or match is None:
        raise CredentialError("Cannot identify OpenCode's version; verify 'opencode --version' works.")
    return int(match[1])


def agent_command(
    agent: str, profile: Profile, token: str, args: list[str], model: str | None = None,
    customer: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build a child-only gateway configuration; tokens never appear in command arguments."""
    env = os.environ.copy()
    env[TOKEN_VARIABLE] = token
    if customer is not None:
        env[CUSTOMER_VARIABLE] = validate_tag(customer)
    if agent == "claude":
        env = claude_environment(profile, model, customer, env)
        env["ANTHROPIC_AUTH_TOKEN"] = token
        command = ["claude"]
    elif agent == "codex":
        env["OPENAI_API_KEY"] = token
        env["OPENAI_BASE_URL"] = profile.gateway + "/v1"
        provider = "llm-credential-mapping"
        settings = {
            "model_provider": provider,
            f"model_providers.{provider}.name": "LiteLLM",
            f"model_providers.{provider}.base_url": profile.gateway + "/v1",
            f"model_providers.{provider}.wire_api": "responses",
            f"model_providers.{provider}.env_key": TOKEN_VARIABLE,
            f"model_providers.{provider}.requires_openai_auth": False,
        }
        if customer is not None:
            settings[f"model_providers.{provider}.env_http_headers.{TAG_HEADER}"] = CUSTOMER_VARIABLE
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
            "name": "LiteLLM",
            "options": {
                "baseURL": profile.gateway + "/v1",
                "apiKey": "{env:" + TOKEN_VARIABLE + "}",
            },
            "models": {model: {"name": model}},
        }
        if customer is not None:
            config["provider"][provider]["options"]["headers"] = {TAG_HEADER: customer}
        config["model"] = provider + "/" + model
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        return _opencode_command(args), env
    else:
        raise CredentialError("Unsupported agent; choose claude, codex or opencode.")
    return command + args, env


def launch(
    agent: str, profile: Profile, token: str, args: list[str], model: str | None,
    customer: str | None = None,
) -> int:
    command, env = agent_command(agent, profile, token, args, model, customer)
    executable = shutil.which(command[0])
    if executable is None:
        raise CredentialError(f"Install '{agent}' and make it available on PATH.")
    if agent == "opencode":
        command = _opencode_command(args, _opencode_major(executable))
    command[0] = executable
    try:
        return subprocess.run(command, env=env, check=False).returncode
    except OSError:
        raise CredentialError(f"Cannot start '{agent}'; check its installation.") from None
