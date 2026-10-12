"""Codex configured once in its user config.toml, with a token command and environment-driven headers.

Codex has no per-repository way to set provider headers (project config may not touch providers), so
attribution header values come from environment variables, set per directory by ``shell-init`` or by
``run codex``. The token command is called with ``--require-attribution-env`` and refuses without them.
Codex then sends the request without a credential, which the gateway rejects: nothing goes through
untagged. Codex reruns the command every ``refresh_interval_ms`` and once after a 401.

The token command must see those variables. Interactive Codex on Windows filters the environment of
the commands it starts unless ``shell_environment_policy.inherit = "all"`` (its documented default) is
set explicitly, so setup and run set it whenever the user's configuration does not choose a value.
"""

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Mapping

from ..adapters import Api, IntegrationContext
from ..customers import Attribution
from ..errors import CredentialError
from ..profiles import atomic_write_text
from .common import CUSTOMER_VARIABLE, attribution_headers, header_variable, run

PROVIDER = "llm-credentials"
REQUIRE_ENV = "--require-attribution-env"
_TOP = ("# >>> llm-credentials: managed by 'llm-credentials setup codex' >>>", "# <<< llm-credentials <<<")
_TABLES = ("# >>> llm-credentials tables: managed by 'llm-credentials setup codex' >>>", "# <<< llm-credentials tables <<<")
_CLEARED = ("OPENAI_API_KEY", "OPENAI_BASE_URL")
INHERIT = "shell_environment_policy.inherit"


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def _without_blocks(text: str) -> str:
    for start, end in (_TOP, _TABLES):
        text = re.sub(rf"\n?{re.escape(start)}\n.*?{re.escape(end)}\n?", "\n", text, flags=re.DOTALL)
    return text.strip("\n") + "\n" if text.strip() else ""


def _toml(value) -> str:
    """A TOML value; JSON strings and arrays of strings are valid TOML."""
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(key)} = {_toml(item)}" for key, item in value.items()) + " }"
    return json.dumps(value)


def _chosen_inherit() -> object:
    """The user's own shell_environment_policy.inherit, or None when their configuration leaves it unset."""
    try:
        parsed = tomllib.loads(_without_blocks(config_path().read_text(encoding="utf-8")))
    except (OSError, UnicodeError, RuntimeError, tomllib.TOMLDecodeError):  # RuntimeError: no home folder.
        return None
    policy = parsed.get("shell_environment_policy")
    return policy.get("inherit") if isinstance(policy, dict) else None


class Codex:
    """Codex through the gateway's OpenAI Responses API."""

    name = "codex"

    def provider(self, context: IntegrationContext) -> dict:
        """The provider settings shared by setup (config.toml) and run (-c overrides)."""
        headers = {name: header_variable(name) for name in context.gateway.attribution_header_names()}
        for name in headers:
            if not re.fullmatch(r"[A-Za-z0-9-]+", name):
                raise CredentialError("The gateway adapter produced an invalid attribution header name.")
        command = context.cli_command("token", "--profile", context.profile.name, REQUIRE_ENV)
        return {
            "name": "llm-credentials",
            "base_url": context.gateway.endpoint(Api.OPENAI_RESPONSES),
            "wire_api": "responses",
            "env_http_headers": headers,
            "auth": {"command": command[0], "args": list(command[1:]), "timeout_ms": 30000, "refresh_interval_ms": 240000},
        }

    # Launch

    def command(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        env = os.environ.copy()
        for name in _CLEARED:
            env.pop(name, None)
        # Header values come from the environment so they never reach argv; the token is never passed.
        for name, value in attribution_headers(context, attribution).items():
            env[header_variable(name)] = value
        if attribution is not None:
            env[CUSTOMER_VARIABLE] = attribution.customer
        settings = {"model_provider": PROVIDER}
        if _chosen_inherit() is None:
            settings[INHERIT] = "all"
        for key, value in self.provider(context).items():
            if isinstance(value, dict):
                settings.update({f"model_providers.{PROVIDER}.{key}.{item}": entry for item, entry in value.items()})
            else:
                settings[f"model_providers.{PROVIDER}.{key}"] = value
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

    # User-level setup

    def _read(self, path: Path) -> tuple[str | None, str, dict]:
        try:
            original = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            original = None
        except (OSError, UnicodeError):
            raise CredentialError(f"Cannot read {path}.") from None
        rest = _without_blocks(original or "")
        try:
            parsed = tomllib.loads(rest)
        except tomllib.TOMLDecodeError:
            raise CredentialError(f"{path} is not valid TOML; fix it first, nothing was changed.") from None
        return original, rest, parsed

    def _write(self, path: Path, text: str, original: str | None) -> None:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            raise CredentialError(f"Could not produce a valid {path}; nothing was changed.") from None
        try:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != original:
                raise CredentialError(f"{path} changed while it was being updated; nothing was changed, retry.")
            permissions = path.stat().st_mode & 0o777 if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, text, permissions=permissions, follow_symlinks=True)
        except OSError:
            raise CredentialError(f"Cannot write {path}; nothing was changed.") from None

    def setup(self, context: IntegrationContext, model: str | None = None, replace: bool = False) -> list[str]:
        path = config_path()
        original, rest, parsed = self._read(path)
        if parsed.get("model_provider") not in (None, PROVIDER):
            raise CredentialError(
                f"{path} already selects model_provider {json.dumps(parsed['model_provider'])}; "
                "remove that line, then rerun setup."
            )
        if PROVIDER in parsed.get("model_providers", {}):
            raise CredentialError(f"{path} already defines [model_providers.{PROVIDER}] outside the managed block.")
        top = [f"model_provider = {json.dumps(PROVIDER)}"]
        notes = []
        policy = parsed.get("shell_environment_policy")
        if policy is None:
            top.append(f'{INHERIT} = "all"')
        elif not isinstance(policy, dict) or policy.get("inherit") != "all":
            notes.append(
                f'Note: {path} has its own [shell_environment_policy]; set inherit = "all" there, otherwise '
                "interactive Codex on Windows does not pass the customer to the token command."
            )
        if model and "model" not in parsed:
            top.append(f"model = {json.dumps(model)}")
        elif model:
            notes.append(f"Note: {path} already sets a model; it was left unchanged.")
        provider = self.provider(context)
        auth = provider.pop("auth")
        tables = [f"[model_providers.{PROVIDER}]"] + [f"{key} = {_toml(value)}" for key, value in provider.items()]
        tables += ["", f"[model_providers.{PROVIDER}.auth]"] + [f"{key} = {_toml(value)}" for key, value in auth.items()]
        text = "\n".join([_TOP[0], *top, _TOP[1], ""]) + (rest + "\n" if rest else "") + "\n".join([_TABLES[0], *tables, _TABLES[1], ""])
        self._write(path, text, original)
        return [
            f"Configured {path} for profile '{context.profile.name}'. Codex renews the credential itself.",
            "Codex reads the customer from the environment: add the hook printed by "
            "'llm-credentials shell-init bash|zsh|fish|pwsh' to your shell profile, or start Codex with "
            "'llm-credentials run codex'. Without a customer, Codex gets no credential and the gateway refuses the request.",
            *notes,
        ]

    def remove(self, context: IntegrationContext) -> list[str]:
        path = config_path()
        original, rest, _ = self._read(path)
        if original is None or not any(marker in original for marker in (_TOP[0], _TABLES[0])):
            return [f"{path} has no llm-credentials configuration."]
        self._write(path, rest, original)
        return [f"Removed the llm-credentials configuration from {path}."]
