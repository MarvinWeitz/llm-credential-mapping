import os
import re
import shlex
import shutil
import subprocess
from typing import Sequence

from ..adapters import IntegrationContext
from ..customers import Attribution
from ..errors import CredentialError

TOKEN_VARIABLE = "LLM_CREDENTIAL_MAPPING_TOKEN"
CUSTOMER_VARIABLE = "LLM_CREDENTIAL_MAPPING_CUSTOMER"


def child_environment(context: IntegrationContext, attribution: Attribution | None) -> dict[str, str]:
    """Copy this process's environment for one child and add its credential and customer; the parent is unchanged."""
    env = os.environ.copy()
    env[TOKEN_VARIABLE] = context.token
    if attribution is not None:
        env[CUSTOMER_VARIABLE] = attribution.customer
    return env


def attribution_headers(context: IntegrationContext, attribution: Attribution | None) -> dict[str, str]:
    """The gateway's headers for this customer; names and values must be safe in any tool's header configuration."""
    if attribution is None:
        return {}
    headers = context.gateway.attribution_headers(attribution)
    for name, value in headers.items():
        if not re.fullmatch(r"[A-Za-z0-9-]+", name) or any(not 32 <= ord(char) <= 126 for char in value):
            raise CredentialError("The gateway adapter produced an invalid attribution header.")
    return headers


def shell_command(argv: Sequence[str]) -> str:
    """Quote a command for tools that run it through a shell.

    On Windows the same string may run under cmd or Git Bash (Claude Code uses Git Bash for hooks), so
    every argument is double-quoted with forward slashes, which both read literally; characters either
    shell would expand inside double quotes are refused.
    """
    if os.name == "nt":
        if any(char in arg for arg in argv for char in '%!"$`\r\n'):
            raise CredentialError(
                "A path in the credential helper command contains shell metacharacters; "
                "install the CLI and keep its configuration in a simple path."
            )
        return " ".join('"' + arg.replace("\\", "/") + '"' for arg in argv)
    return shlex.join(argv)


def header_variable(name: str) -> str:
    """A fixed environment variable for one attribution header, for tools that read header values from the environment."""
    return "LLM_CREDENTIALS_HEADER_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def run(name: str, command: list[str], env: dict[str, str]) -> int:
    """Start the installed tool with a child-only environment and return its exit status."""
    executable = shutil.which(command[0])
    if executable is None:
        raise CredentialError(f"Install '{name}' and make it available on PATH.")
    return execute(name, [executable, *command[1:]], env)


def execute(name: str, command: list[str], env: dict[str, str]) -> int:
    try:
        return subprocess.run(command, env=env, check=False).returncode
    except OSError:
        raise CredentialError(f"Cannot start '{name}'; check its installation.") from None
