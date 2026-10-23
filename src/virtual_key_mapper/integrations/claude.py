"""Claude Code (CLI and the official VS Code extension) configured through its own settings files.

User level (``setup claude``): ``apiKeyHelper`` runs ``vkm token``, so Claude renews the
credential itself; ``env.ANTHROPIC_BASE_URL`` points at the gateway, and
``env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`` lets ``/model`` list the gateway's models; a
``UserPromptSubmit`` hook runs ``vkm claude guard`` before every prompt.

Per repository: the guard writes the gateway's attribution headers to ``.claude/settings.local.json``
(``env.ANTHROPIC_CUSTOM_HEADERS``), which Claude reloads live, and blocks prompts that would be sent
without the folder's labels. Claude reads that file at the Git root, except on Windows and outside
Git, where it uses the session's starting directory.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping

from ..adapters import Api, IntegrationContext
from ..errors import CredentialError
from ..labels import Attribution
from ..profiles import atomic_write_text
from ..repositories import Checkout
from ..resolve import missing_error
from .common import attribution_headers, run, set_launch_marker, shell_command

LOCAL_SETTINGS = Path(".claude") / "settings.local.json"
_MARKER = "virtual_key_mapper"
# Claude Code then lists the gateway's models (GET /v1/models with the helper's token) in /model.
DISCOVERY = "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"
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
        # Any *api-key header: gateways read their own key header (LiteLLM's even before Authorization).
        if name.lower() in ("authorization", "host") or name.lower().endswith("api-key"):
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
    except (ValueError, RecursionError):
        raise CredentialError(f"{path} is not valid JSON; fix it first, nothing was changed.") from None
    if not isinstance(value, dict) or not isinstance(value.get("env", {}), dict):
        raise CredentialError(f"{path} must contain a JSON object with an object-valued env; nothing was changed.")
    return raw, value


def _write_json(path: Path, value: dict, original: str | None, follow_symlinks: bool = False) -> None:
    """Replace ``path`` atomically, keeping its permissions, unless someone changed it since it was read."""
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != original:
            raise CredentialError(f"{path} changed while it was being updated; nothing was changed, retry.")
        permissions = path.stat().st_mode & 0o777 if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path, json.dumps(value, indent=2) + "\n", permissions=permissions, follow_symlinks=follow_symlinks,
        )
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
        # Replace every header the adapter can send: an inherited one this launch omits must not survive.
        headers = _claude_headers(
            env.get("ANTHROPIC_CUSTOM_HEADERS", ""), attribution_headers(context, attribution),
            set(context.labels.header_names()),
        )
        if DISCOVERY not in env:
            settings["env"][DISCOVERY] = "1"
        if headers or "ANTHROPIC_CUSTOM_HEADERS" in env:
            settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = headers
        if model:
            settings["model"] = model
        for name in _CLEARED:
            env.pop(name, None)  # They would take precedence over the renewing helper.
        set_launch_marker(env, attribution)
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

    def _load_state(self, context: IntegrationContext) -> dict:
        """What the last setup wrote; empty when unknown."""
        try:
            state = json.loads(self._state(context).read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return {}
        return state if isinstance(state, dict) else {}

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
        notes = [
            f"Configured {path} for profile '{context.profile.name}'. New Claude sessions renew credentials "
            "themselves and get the labels of each repository; running sessions pick it up on the next prompt. "
            "/model lists the gateway's models from the next start.",
            'For the VS Code extension, add to your VS Code user settings: {"claudeCode.disableLoginPrompt": true}',
        ]
        # Record only values this setup owns, so remove never deletes the user's own (identical) ones.
        previous = self._load_state(context)
        owns_url = env.get("ANTHROPIC_BASE_URL") != base_url or previous.get("base_url") == base_url
        data["apiKeyHelper"] = self.helper(context)
        env["ANTHROPIC_BASE_URL"] = base_url
        # A value of the user's own (such as "0") stays; ours is the "1" this or an earlier setup wrote.
        owns_discovery = DISCOVERY not in env or (previous.get("discovery") is True and env[DISCOVERY] == "1")
        if owns_discovery:
            env[DISCOVERY] = "1"
        if model and data.get("model") in (None, previous.get("model")):
            data["model"] = model
        elif model:
            if data["model"] != model:
                notes.append(f"Note: {path} already sets a model; it was left unchanged.")
            model = None
        elif previous.get("model") is not None and data.get("model") == previous["model"]:
            model = previous["model"]  # Still ours from an earlier setup: keep it so remove can undo it.
        hooks["UserPromptSubmit"] = [entry for entry in hooks.get("UserPromptSubmit", []) if not _our_hook(entry)] + [
            {"hooks": [{"type": "command", "command": self.guard_command(context)}]},
        ]
        _write_json(path, data, original, follow_symlinks=True)
        state = {
            "version": 1, "settings": str(path), "profile": context.profile.name,
            "base_url": base_url if owns_url else None, "model": model, "discovery": owns_discovery,
        }
        atomic_write_text(self._state(context), json.dumps(state, indent=2) + "\n")
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            if os.environ.get(name):
                notes.append(f"Warning: {name} is set in this environment and takes precedence over the helper; unset it.")
        return notes

    def remove(self, context: IntegrationContext) -> list[str]:
        """Undo setup: remove only values that are still the ones setup wrote."""
        path = settings_path()
        original, data = _read_json(path)
        state_path = self._state(context)
        state = self._load_state(context)
        env = data.get("env", {})
        helper_was_ours = _ours(data.get("apiKeyHelper"))
        if helper_was_ours:
            del data["apiKeyHelper"]
        if env.get("ANTHROPIC_BASE_URL") is not None and (
            env.get("ANTHROPIC_BASE_URL") == state.get("base_url") or (helper_was_ours and not state)
        ):
            del env["ANTHROPIC_BASE_URL"]
        if state.get("discovery") is True and env.get(DISCOVERY) == "1":
            del env[DISCOVERY]
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
            _write_json(path, data, original, follow_symlinks=True)
        state_path.unlink(missing_ok=True)
        return [
            f"Removed the vkm configuration from {path}. Restart Claude sessions; "
            "remove claudeCode.disableLoginPrompt from VS Code to get Claude's own sign-in back.",
        ]

    # Per-repository attribution

    def local_settings(self, directory: Path, root: Path | None) -> Path:
        """Where Claude reads the local settings for a session started in ``directory``."""
        return (directory if os.name == "nt" or root is None else root) / LOCAL_SETTINGS

    def _written(self, context: IntegrationContext, path: Path) -> dict[str, str]:
        names = {name.lower() for name in context.labels.header_names()}
        _, data = _read_json(path)
        value = data.get("env", {}).get("ANTHROPIC_CUSTOM_HEADERS", "")
        return _header_values(value, names) if isinstance(value, str) else {}

    def write_local(
        self, context: IntegrationContext, path: Path, attribution: Attribution | None, root: Path | None,
    ) -> None:
        """Set (or, with nothing to send, remove) the attribution headers in a local settings file; keep everything else."""
        original, data = _read_json(path)
        env = data.setdefault("env", {})
        existing = env.get("ANTHROPIC_CUSTOM_HEADERS", "")
        if not isinstance(existing, str):
            raise CredentialError(f"{path} has an invalid ANTHROPIC_CUSTOM_HEADERS; nothing was changed.")
        value = _claude_headers(
            existing, attribution_headers(context, attribution), set(context.labels.header_names()),
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
        """After a rebind, update a local settings file this tool wrote; leave other files alone.

        While required labels are missing the headers are removed, so the guard keeps blocking.
        """
        if not path.is_file() or not self._written(context, path):
            return False
        complete = attribution is not None and attribution.complete
        self.write_local(context, path, attribution if complete else None, root)
        return True

    def guard(
        self, context: IntegrationContext, directory: Path, checkout: Checkout | None, attribution: Attribution,
        environ: Mapping[str, str],
    ) -> str | None:
        """Decide a prompt before it is sent: None allows it, a string blocks it with that reason.

        ``attribution`` is the directory resolved with the launch marker's labels as explicit, so a
        session that ``run claude --label`` started passes with its own labels. Allows a session whose
        headers equal the computed ones (an absent header equals an omitted one). Otherwise writes the
        local settings file and blocks once, because Claude only uses the reloaded file from the next
        prompt. Other headers are stale: Claude keeps a header in its environment after the file stops
        setting it. When the launch marker sets any label, the file (shared by every session in the
        folder) is never written: launch-only labels are compared with the session's headers only.
        """
        root = checkout.root if checkout is not None else None
        path = self.local_settings(directory, root)
        launched = any(label.source == "explicit" for label in attribution.labels)
        written = {} if launched else self._written(context, path)
        if not attribution.complete:
            if written:
                self.write_local(context, path, None, root)
            return str(missing_error(attribution, directory, checkout))
        names = {name.lower() for name in context.labels.header_names()}
        present = _header_values(environ.get("ANTHROPIC_CUSTOM_HEADERS", ""), names)
        expected = {name.lower(): value for name, value in attribution_headers(context, attribution).items()}
        if present == expected:
            return None
        if launched:
            return (
                f"{_describe(attribution)} were chosen at launch, but this session does not send them. "
                "Start it again with 'vkm run claude'; nothing was sent."
            )
        if written == expected:
            return (
                f"{_describe(attribution)} set in {path}, but this session has not loaded it. "
                "Send the prompt again; if this repeats, trust the folder or start a new session there."
            )
        self.write_local(context, path, attribution, root)
        sources = "; ".join(
            f"{label.key}: {label.source} {label.detail}" for label in attribution.labels if label.value is not None
        )
        return (
            f"{_describe(attribution)} assigned{f' ({sources})' if sources else ''}. "
            "Send the prompt again so it is attributed; nothing was sent."
        )


def _describe(attribution: Attribution) -> str:
    values = attribution.values
    if not values:
        return "No label values are"
    return "Labels " + ", ".join(f"{key}={json.dumps(value)}" for key, value in values.items())
