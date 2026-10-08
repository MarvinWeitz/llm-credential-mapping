import json
import os
import re
import shutil
import subprocess

from ..adapters import Api, IntegrationContext
from ..customers import Attribution
from ..errors import CredentialError
from .common import TOKEN_VARIABLE, attribution_headers, child_environment, execute

PROVIDER = "llm-credential-mapping"


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


class OpenCode:
    """OpenCode through the gateway's OpenAI Chat Completions API, as an inline provider."""

    name = "opencode"

    def command(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        if not model:
            raise CredentialError("OpenCode requires --model with a model alias available on the gateway.")
        env = child_environment(context, attribution)
        try:
            config = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        except ValueError:
            raise CredentialError("Existing OPENCODE_CONFIG_CONTENT is invalid JSON.") from None
        if not isinstance(config, dict) or not isinstance(config.get("provider", {}), dict):
            raise CredentialError("Existing OpenCode provider configuration is invalid.")
        options = {
            "baseURL": context.gateway.endpoint(Api.OPENAI_CHAT),
            "apiKey": "{env:" + TOKEN_VARIABLE + "}",
        }
        headers = attribution_headers(context, attribution)
        if headers:
            options["headers"] = headers
        config.setdefault("provider", {})[PROVIDER] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "llm-credentials",
            "options": options,
            "models": {model: {"name": model}},
        }
        config["model"] = PROVIDER + "/" + model
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        return _opencode_command(args), env

    def launch(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> int:
        _, env = self.command(context, attribution, args, model)
        executable = shutil.which("opencode")
        if executable is None:
            raise CredentialError("Install 'opencode' and make it available on PATH.")
        command = _opencode_command(args, _opencode_major(executable))
        return execute(self.name, [executable, *command[1:]], env)
