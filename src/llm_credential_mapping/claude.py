import json
import os
import shlex
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from .agents import CUSTOMER_VARIABLE, TOKEN_VARIABLE, claude_environment
from .errors import CredentialError
from .litellm import required_text
from .profiles import Profile, ProfileStore, atomic_write_json, default_home
from .repositories import RepositoryStore, current_repository
from .sessions import SessionManager
from .storage import NativeKeyring
from .tags import validate_tag

CONTEXT_VARIABLE = "LLM_CREDENTIAL_MAPPING_CLAUDE_CONTEXT"
_CLEARED_VARIABLES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", TOKEN_VARIABLE,
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)
_MODEL_VARIABLES = (
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
)


@dataclass(frozen=True)
class ClaudeConfiguration:
    """The authentication profile and model alias used by future official-extension sessions."""

    profile: str
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.profile, str):
            raise CredentialError("Claude's authentication profile name must be a string.")
        Profile(self.profile, "https://validation.example", "validation")
        if (
            not isinstance(self.model, str) or not self.model.strip() or self.model != self.model.strip()
            or len(self.model) > 256 or any(char.isspace() or ord(char) < 32 for char in self.model)
        ):
            raise CredentialError("Provide a nonempty LiteLLM model alias without whitespace.")


@dataclass(frozen=True)
class ClaudeContext:
    """Nonsecret session snapshot; helpers reuse its profile/employee, never the current global selection or repo binding."""

    home: str
    profile: str
    account: str
    user_id: str
    repository: str
    customer: str
    model: str

    def encode(self) -> str:
        return json.dumps({"version": 1, **self.__dict__})

    @classmethod
    def from_environment(cls) -> "ClaudeContext":
        try:
            data = json.loads(os.environ.get(CONTEXT_VARIABLE, ""))
        except ValueError:
            raise CredentialError("Claude credential helper has no valid pinned context; start it through the wrapper.") from None
        if (
            not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or set(data) != {"version", "home", "profile", "account", "user_id", "repository", "customer", "model"}
        ):
            raise CredentialError("Invalid pinned Claude context; start a new session through the wrapper.")
        for name in ("home", "profile", "account", "user_id", "repository", "customer", "model"):
            required_text(data, name)
        if not Path(data["home"]).is_absolute():
            raise CredentialError("Pinned Claude configuration home must be absolute.")
        ClaudeConfiguration(data["profile"], data["model"])
        validate_tag(data["customer"])
        return cls(**{key: value for key, value in data.items() if key != "version"})


def wrapper_path() -> Path:
    return Path(sysconfig.get_path("scripts")) / ("llm-credentials-claude.exe" if os.name == "nt" else "llm-credentials-claude")


def verify_wrapper() -> Path:
    executable = wrapper_path()
    if not executable.is_file():
        raise CredentialError("The Claude wrapper is not installed. Reinstall this package with 'python -m pip install -e .'.")
    try:
        result = subprocess.run(
            [str(executable), sys.executable, "--version"], capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise CredentialError(
            "Cannot execute the installed Claude wrapper on this host. "
            "Check executable permissions and your host's application policy; no settings were changed."
        ) from None
    if result.returncode != 0:
        raise CredentialError("Claude wrapper self-check failed. Check its installation before configuring VS Code.")
    return executable


def load_configuration(store: ProfileStore) -> ClaudeConfiguration:
    with store.locked("claude-integration"):
        try:
            data = json.loads((store.home / "claude.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CredentialError("Run 'llm-credentials claude setup --profile NAME --model ALIAS' on this host first.") from None
        if (
            not isinstance(data, dict) or set(data) != {"version", "profile", "model"}
            or type(data["version"]) is not int or data["version"] != 1
        ):
            raise CredentialError("Invalid claude.json; rerun 'claude setup'.")
        return ClaudeConfiguration(data["profile"], data["model"])


def resolve_context(
    store: ProfileStore, configuration: ClaudeConfiguration, directory: Path | None = None,
) -> tuple[ClaudeContext, Profile]:
    profile = store.get(configuration.profile)
    repository = current_repository(directory)
    customer = RepositoryStore(store).get(profile.gateway, repository)
    if customer is None:
        raise CredentialError(
            "This repository has no customer binding for the selected gateway. "
            "Run 'llm-credentials repo bind --profile " + profile.name + "' in its terminal first."
        )
    manager = SessionManager(store, NativeKeyring())
    status = manager.status(profile)
    if status["state"] == "logged-out":
        raise CredentialError(f"No login for '{profile.name}'; run login --profile {profile.name} in this host's terminal.")
    user_id = status["user_id"]
    manager.token(profile, expected_user=user_id)
    return ClaudeContext(
        str(store.home.resolve()), profile.name, profile.account, user_id, repository, customer, configuration.model,
    ), profile


def setup(
    store: ProfileStore, profile_name: str | None, model: str,
) -> dict:
    """Validate repo/vault access, save nonsecret adapter config, and return VS Code settings to merge; no editor files are changed."""
    configuration = ClaudeConfiguration(store.get(profile_name).name, model)
    executable = verify_wrapper()
    resolve_context(store, configuration)
    helper_command()
    with store.locked("claude-integration"):
        atomic_write_json(store.home / "claude.json", {"version": 1, **configuration.__dict__})
    return {
        "claudeCode.claudeProcessWrapper": str(executable),
        "claudeCode.disableLoginPrompt": True,
        "claudeCode.environmentVariables": [
            {"name": "LLM_CREDENTIAL_MAPPING_HOME", "value": str(store.home.resolve())},
        ],
    }


def check(store: ProfileStore, directory: Path | None = None) -> dict:
    """Resolve the current repo and verify/renew its credential, returning only nonsecret diagnostic metadata."""
    executable = verify_wrapper()
    helper_command()
    directory = directory.expanduser().resolve() if directory is not None else Path.cwd()
    context, profile = resolve_context(store, load_configuration(store), directory)
    return {
        "profile": context.profile, "gateway": profile.gateway, "team_id": profile.team_id,
        "user_id": context.user_id, "repository": context.repository, "customer": context.customer,
        "model": context.model, "wrapper": str(executable), "lookup_directory": str(directory), "state": "ready",
    }


def helper_token() -> str:
    """Return the pinned employee's credential to Claude; reject changed profiles/users and never open a browser."""
    context = ClaudeContext.from_environment()
    store = ProfileStore(Path(context.home))
    profile = store.get(context.profile)
    if profile.account != context.account:
        raise CredentialError("Claude's authentication profile changed; start a new session. No credential returned.")
    return SessionManager(store, NativeKeyring()).token(profile, expected_user=context.user_id)


def helper_command() -> str:
    parts = [sys.executable, "-I", "-m", "llm_credential_mapping", "claude", "token"]
    if os.name == "nt":
        if any(char in sys.executable for char in '%!^&|<>"\r\n'):
            raise CredentialError("Python's path contains shell metacharacters; install the CLI in a simple path for Claude's helper.")
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _settings(value: str) -> dict:
    try:
        if value.lstrip().startswith("{"):
            raw = value
        else:
            path = Path(value)
            if not path.is_file() or path.stat().st_size > 2_097_152:
                raise CredentialError("Claude --settings must name a regular JSON file no larger than 2 MiB.")
            raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        raise CredentialError("Cannot read Claude's --settings JSON; no settings were discarded.") from None
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise CredentialError("Claude's --settings must contain an object and an object-valued env.")
    if any(not isinstance(value, str) for value in data.get("env", {}).values()):
        raise CredentialError("Claude's --settings env values must be strings.")
    return data


def wrapped_command(
    executable: str, arguments: list[str], context: ClaudeContext, profile: Profile,
) -> tuple[list[str], dict[str, str]]:
    """Preserve Claude's stdio arguments/settings while pinning routing/model/tags and adding a token helper, without token argv."""
    forwarded: list[str] = []
    settings: dict = {}
    literal: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            literal = arguments[index:]
            break
        option, separator, value = argument.partition("=")
        if option in ("--settings", "--model", "--fallback-model"):
            if not separator:
                index += 1
                if index >= len(arguments):
                    raise CredentialError(f"Claude {option} is missing its value.")
                value = arguments[index]
            if option == "--settings":
                update = _settings(value)
                merged_env = {**settings.get("env", {}), **update.get("env", {})}
                settings.update(update)
                settings["env"] = merged_env
            elif option == "--fallback-model":
                forwarded.extend([option, context.model])
        else:
            forwarded.append(argument)
        index += 1
    inherited = {**os.environ, **settings.get("env", {})}
    env = claude_environment(profile, context.model, context.customer, inherited)
    overrides = {
        "ANTHROPIC_BASE_URL": profile.gateway,
        "ANTHROPIC_CUSTOM_HEADERS": env["ANTHROPIC_CUSTOM_HEADERS"],
        CONTEXT_VARIABLE: context.encode(),
        CUSTOMER_VARIABLE: context.customer,
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "60000",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        **{name: context.model for name in _MODEL_VARIABLES},
        **{name: "" for name in _CLEARED_VARIABLES},
    }
    env.update(overrides)
    settings["env"] = {**settings.get("env", {}), **overrides}
    settings["apiKeyHelper"] = helper_command()
    settings["model"] = context.model
    command = [
        executable, *forwarded, "--model", context.model,
        "--settings", json.dumps(settings), *literal,
    ]
    return command, env


def _execute(command: list[str], env: dict[str, str]) -> int:
    if os.name != "nt":
        os.execvpe(command[0], command, env)
        raise CredentialError("Claude process replacement unexpectedly returned.")
    from .windows_process import execute
    return execute(command, env)


def project_directory(arguments: list[str], cwd: Path) -> Path:
    """Use the SDK's explicit project-config root, or cwd; never infer a repository from saved mappings or child directories."""
    root = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        option, separator, value = argument.partition("=")
        if option == "--project-config-root":
            if not separator:
                index += 1
                if index >= len(arguments) or arguments[index].startswith("--"):
                    raise CredentialError("Claude --project-config-root is missing its directory.")
                value = arguments[index]
            if root is not None:
                raise CredentialError("Claude supplied multiple project roots; customer attribution is ambiguous.")
            if not value or any(ord(char) < 32 for char in value):
                raise CredentialError("Claude supplied an invalid project root directory.")
            root = Path(value).expanduser()
        index += 1
    if root is None:
        return cwd
    return (root if root.is_absolute() else cwd / root).resolve()


def wrapper_main(argv: list[str] | None = None) -> int:
    """Official extension executable hook: first argument is its bundled Claude binary; stdout belongs exclusively to Claude."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["--llm-check"]:
            print(json.dumps(check(ProfileStore(default_home())), indent=2))
            return 0
        if not arguments or not Path(arguments[0]).is_absolute() or not Path(arguments[0]).is_file():
            raise CredentialError("The official Claude extension must pass its bundled executable to this wrapper.")
        executable, forwarded = arguments[0], arguments[1:]
        if forwarded in (["--version"], ["-v"], ["--help"], ["-h"]):
            return _execute(arguments, os.environ.copy())
        store = ProfileStore(default_home())
        cwd = Path.cwd()
        directory = project_directory(forwarded, cwd)
        configuration = load_configuration(store)
        try:
            context, profile = resolve_context(store, configuration, directory)
        except CredentialError as error:
            mode = "configuration-probe" if os.environ.get("CLAUDE_CODE_CONFIG_PROBE") == "1" else "session"
            raise CredentialError(
                f"{error} Claude process cwd={json.dumps(str(cwd))}; "
                f"Git lookup directory={json.dumps(str(directory))}; mode={mode}. "
                "No fallback repository or customer was selected."
            ) from None
        command, env = wrapped_command(executable, forwarded, context, profile)
        print(
            f"LLM credentials: profile {json.dumps(context.profile)}, customer {json.dumps(context.customer)}, "
            f"repository {json.dumps(context.repository)}. Context pinned for this process.",
            file=sys.stderr,
        )
        return _execute(command, env)
    except CredentialError as error:
        print(f"LLM Claude adapter: {error} Run 'llm-credentials claude check' on the extension host.", file=sys.stderr)
        return 1
    except OSError:
        print("LLM Claude adapter: cannot launch Claude; check executable permissions and installation.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
