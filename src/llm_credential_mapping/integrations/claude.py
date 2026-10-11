"""Claude Code (CLI and the official VS Code extension) configured through its own settings files.

User level (``setup claude``): ``apiKeyHelper`` runs ``llm-credentials token``, so Claude renews the
credential itself; ``env.ANTHROPIC_BASE_URL`` points at the gateway; a ``UserPromptSubmit`` hook runs
``llm-credentials claude guard`` before every prompt.

Per repository: the guard writes the gateway's attribution headers to ``.claude/settings.local.json``
(``env.ANTHROPIC_CUSTOM_HEADERS``), which Claude reloads live, and blocks prompts that would be sent
without a customer. Claude reads that file at the Git root, except on Windows and outside Git, where
it uses the session's starting directory.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping

from ..adapters import Api, IntegrationContext
from ..customers import Attribution
from ..errors import CredentialError
from ..profiles import atomic_write_text
from .common import CUSTOMER_VARIABLE, attribution_headers, run, shell_command

LOCAL_SETTINGS = Path(".claude") / "settings.local.json"
_MARKER = "llm_credential_mapping"
_CLEARED = (
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)


def _claude_headers(existing: str, headers: dict[str, str], replaced: set[str] | None = None) -> str:
    """Merge attribution headers into ANTHROPIC_CUSTOM_HEADERS, replacing ``replaced`` names and keeping unrelated ones."""
    replaced = {name.lower() for name in (replaced if replaced is not None else headers)}
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


def _header_values(value: str, names: set[str]) -> dict[str, str]:
    """The given headers (lowercase names) as set in an ANTHROPIC_CUSTOM_HEADERS value; malformed lines are ignored."""
    found = {}
    for line in value.splitlines():
        name, separator, content = line.partition(":")
        if separator and name.strip().lower() in names:
            found[name.strip().lower()] = content.strip()
    return found


def settings_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def _read_json(path: Path) -> tuple[str | None, dict]:
    """Return the raw text (None if missing) and the parsed object; refuse to guess about invalid files."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, {}
    except (OSError, UnicodeError):
        raise CredentialError(f"Cannot read {path}.") from None
    try:
        value = json.loads(raw)
    except ValueError:
        raise CredentialError(f"{path} is not valid JSON; fix it first, nothing was changed.") from None
    if not isinstance(value, dict) or not isinstance(value.get("env", {}), dict):
        raise CredentialError(f"{path} must contain a JSON object with an object-valued env; nothing was changed.")
    return raw, value


def _write_json(path: Path, value: dict, original: str | None) -> None:
    """Replace ``path`` atomically, keeping its permissions, unless someone changed it since it was read."""
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != original:
            raise CredentialError(f"{path} changed while it was being updated; nothing was changed, retry.")
        permissions = path.stat().st_mode & 0o777 if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(value, indent=2) + "\n", permissions=permissions)
    except OSError:
        raise CredentialError(f"Cannot write {path}; nothing was changed.") from None


def _ours(command: object) -> bool:
    return isinstance(command, str) and _MARKER in command


def _our_hook(entry: object) -> bool:
    return isinstance(entry, dict) and any(
        isinstance(hook, dict) and _ours(hook.get("command")) for hook in entry.get("hooks", [])
    )


def _exclude_from_git(root: Path, path: Path) -> None:
    """Add a generated file to the checkout's info/exclude unless Git already ignores it; best effort."""
    try:
        relative = path.relative_to(root).as_posix()
        if subprocess.run(
            ["git", "check-ignore", "-q", relative], cwd=root, capture_output=True, timeout=10, check=False,
        ).returncode == 0:
            return
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"], cwd=root, capture_output=True, text=True,
            timeout=10, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        exclude = root / result.stdout.strip()
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        separator = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as output:
            output.write(f"{separator}/{relative}\n")
    except (OSError, ValueError, subprocess.SubprocessError, UnicodeError):
        pass


class Claude:
    """Claude Code through the gateway's Anthropic Messages API."""

    name = "claude"

    # Settings shared by the CLI launcher and the user-level setup

    def helper(self, context: IntegrationContext) -> str:
        return shell_command(context.cli_command("token", "--profile", context.profile.name))

    def guard_command(self, context: IntegrationContext) -> str:
        return shell_command(context.cli_command("claude", "guard", "--profile", context.profile.name))

    # Launch

    def command(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        """Run claude with one-off --settings (outranking project and local files); the token is never passed."""
        env = os.environ.copy()
        settings = {
            "apiKeyHelper": self.helper(context),
            "env": {"ANTHROPIC_BASE_URL": context.gateway.endpoint(Api.ANTHROPIC_MESSAGES)},
        }
        headers = attribution_headers(context, attribution)
        if headers:
            settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = _claude_headers(env.get("ANTHROPIC_CUSTOM_HEADERS", ""), headers)
        if model:
            settings["model"] = model
        for name in _CLEARED:
            env.pop(name, None)  # They would take precedence over the renewing helper.
        if attribution is not None:
            env[CUSTOMER_VARIABLE] = attribution.customer
        return ["claude", "--settings", json.dumps(settings), *args], env

    def launch(
        self, context: IntegrationContext, attribution: Attribution | None, args: list[str], model: str | None,
    ) -> int:
        return run(self.name, *self.command(context, attribution, args, model))

    # User-level setup

    def _state(self, context: IntegrationContext) -> Path:
        if context.home is None:
            raise CredentialError("Claude setup needs the configuration folder.")
        return context.home / "claude.json"

    def setup(self, context: IntegrationContext, model: str | None = None, replace: bool = False) -> list[str]:
        """Configure Claude's user settings for this profile; refuse to replace another tool's credential setup."""
        path = settings_path()
        original, data = _read_json(path)
        env = data.setdefault("env", {})
        base_url = context.gateway.endpoint(Api.ANTHROPIC_MESSAGES)
        helper = data.get("apiKeyHelper")
        if not replace and helper is not None and not _ours(helper):
            raise CredentialError(f"{path} already has another apiKeyHelper; rerun with --replace to replace it.")
        if not replace and env.get("ANTHROPIC_BASE_URL") not in (None, base_url) and not _ours(helper):
            raise CredentialError(f"{path} already routes Claude to another ANTHROPIC_BASE_URL; rerun with --replace.")
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict) or not isinstance(hooks.get("UserPromptSubmit", []), list):
            raise CredentialError(f"{path} has invalid hooks; nothing was changed.")
        data["apiKeyHelper"] = self.helper(context)
        env["ANTHROPIC_BASE_URL"] = base_url
        if model:
            data["model"] = model
        hooks["UserPromptSubmit"] = [entry for entry in hooks.get("UserPromptSubmit", []) if not _our_hook(entry)] + [
            {"hooks": [{"type": "command", "command": self.guard_command(context)}]},
        ]
        _write_json(path, data, original)
        state = {"version": 1, "settings": str(path), "profile": context.profile.name, "base_url": base_url, "model": model}
        atomic_write_text(self._state(context), json.dumps(state, indent=2) + "\n")
        notes = [
            f"Configured {path} for profile '{context.profile.name}'. New Claude sessions renew credentials "
            "themselves and get the customer of each repository; running sessions pick it up on the next prompt.",
            'For the VS Code extension, add to your VS Code user settings: {"claudeCode.disableLoginPrompt": true}',
        ]
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            if os.environ.get(name):
                notes.append(f"Warning: {name} is set in this environment and takes precedence over the helper; unset it.")
        return notes

    def remove(self, context: IntegrationContext) -> list[str]:
        """Undo setup: remove only values that are still the ones setup wrote."""
        path = settings_path()
        original, data = _read_json(path)
        state_path = self._state(context)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        env = data.get("env", {})
        helper_was_ours = _ours(data.get("apiKeyHelper"))
        if helper_was_ours:
            del data["apiKeyHelper"]
        if env.get("ANTHROPIC_BASE_URL") is not None and (
            env.get("ANTHROPIC_BASE_URL") == state.get("base_url") or (helper_was_ours and not state)
        ):
            del env["ANTHROPIC_BASE_URL"]
        if state.get("model") and data.get("model") == state.get("model"):
            del data["model"]
        hooks = data.get("hooks")
        if isinstance(hooks, dict) and isinstance(hooks.get("UserPromptSubmit"), list):
            hooks["UserPromptSubmit"] = [entry for entry in hooks["UserPromptSubmit"] if not _our_hook(entry)]
            if not hooks["UserPromptSubmit"]:
                del hooks["UserPromptSubmit"]
            if not hooks:
                del data["hooks"]
        if "env" in data and not data["env"]:
            del data["env"]
        if original is not None:
            _write_json(path, data, original)
        state_path.unlink(missing_ok=True)
        return [
            f"Removed the llm-credentials configuration from {path}. Restart Claude sessions; "
            "remove claudeCode.disableLoginPrompt from VS Code to get Claude's own sign-in back.",
        ]

    # Per-repository attribution

    def local_settings(self, directory: Path, root: Path | None) -> Path:
        """Where Claude reads the local settings for a session started in ``directory``."""
        return (directory if os.name == "nt" or root is None else root) / LOCAL_SETTINGS

    def _written(self, context: IntegrationContext, path: Path) -> dict[str, str]:
        names = {name.lower() for name in context.gateway.attribution_header_names()}
        _, data = _read_json(path)
        value = data.get("env", {}).get("ANTHROPIC_CUSTOM_HEADERS", "")
        return _header_values(value, names) if isinstance(value, str) else {}

    def write_local(
        self, context: IntegrationContext, path: Path, attribution: Attribution | None, root: Path | None,
    ) -> None:
        """Set (or, with no attribution, remove) the attribution headers in a local settings file; keep everything else."""
        original, data = _read_json(path)
        env = data.setdefault("env", {})
        existing = env.get("ANTHROPIC_CUSTOM_HEADERS", "")
        if not isinstance(existing, str):
            raise CredentialError(f"{path} has an invalid ANTHROPIC_CUSTOM_HEADERS; nothing was changed.")
        value = _claude_headers(
            existing, attribution_headers(context, attribution), set(context.gateway.attribution_header_names()),
        )
        if value:
            env["ANTHROPIC_CUSTOM_HEADERS"] = value
        else:
            env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
            if not env:
                del data["env"]
        _write_json(path, data, original)
        if root is not None:
            _exclude_from_git(root, path)

    def refresh_local(self, context: IntegrationContext, path: Path, attribution: Attribution | None, root: Path | None) -> bool:
        """After a rebind, update a local settings file this tool wrote; leave other files alone."""
        if not path.is_file() or not self._written(context, path):
            return False
        self.write_local(context, path, attribution, root)
        return True

    def guard(
        self, context: IntegrationContext, directory: Path, root: Path | None, attribution: Attribution | None,
        unmapped: str, environ: Mapping[str, str],
    ) -> str | None:
        """Decide a prompt before it is sent: None allows it, a string blocks it with that reason.

        Allows a session whose headers already match the customer, or whose attribution headers come
        from elsewhere (e.g. ``claude --settings``, a deliberate override). Otherwise writes the local
        settings file and blocks once, because Claude only uses the reloaded file from the next prompt.
        """
        names = {name.lower() for name in context.gateway.attribution_header_names()}
        present = _header_values(environ.get("ANTHROPIC_CUSTOM_HEADERS", ""), names)
        path = self.local_settings(directory, root)
        written = self._written(context, path)
        if present and present != written:
            return None
        if attribution is None:
            if written:
                self.write_local(context, path, None, root)
            return unmapped
        expected = {name.lower(): value for name, value in attribution_headers(context, attribution).items()}
        if present == expected:
            return None
        if written == expected:
            return (
                f"Customer {json.dumps(attribution.customer)} is set in {path}, but this session has not loaded it. "
                "Send the prompt again; if this repeats, trust the folder or start a new session there."
            )
        self.write_local(context, path, attribution, root)
        return (
            f"Customer {json.dumps(attribution.customer)} assigned ({attribution.source}: {attribution.detail}). "
            "Send the prompt again so it is attributed; nothing was sent."
        )
