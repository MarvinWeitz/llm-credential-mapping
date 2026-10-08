import json
import os
import shlex
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

# Legacy VS Code wrapper, deleted in plan step 4; it may use the LiteLLM modules directly.
from .adapters import Api
from .auth.litellm_oauth import required_text, team_id
from .auth.litellm_sso import SessionManager
from .customers import Attribution, validate_customer
from .errors import CredentialError
from .integrations.claude import claude_environment
from .integrations.common import CUSTOMER_VARIABLE, TOKEN_VARIABLE
from .profiles import Profile, ProfileStore, atomic_write_json, default_home, validate_profile_name
from .registry import gateway_for
from .request_context import resolve_identity, validate_model
from .repositories import current_repository
from .storage import NativeKeyring
from .workspaces import WorkspaceStore

CONTEXT_VARIABLE = "LLM_CREDENTIAL_MAPPING_CLAUDE_CONTEXT"
_CLEARED_VARIABLES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", TOKEN_VARIABLE,
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)
_MODEL_VARIABLES = (
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
)


@dataclass(frozen=True)
class ClaudeConfiguration:
    """The authentication profile and model alias used by future official-extension sessions."""

    profile: str
    model: str

    def __post_init__(self) -> None:
        validate_profile_name(self.profile)
        validate_model(self.model)


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
        validate_customer(data["customer"])
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
    expected_repository: str | None = None,
) -> tuple[ClaudeContext, Profile]:
    """Resolve customer/employee context; a supplied origin identity must match before any vault access."""
    profile = store.get(configuration.profile)
    try:
        repository = current_repository(directory)
    except CredentialError as error:
        if expected_repository is None:
            raise
        raise CredentialError(
            f"Cannot validate this workspace's primary checkout: {error} "
            "Verify its path and rerun 'claude workspace bind'; no customer was selected."
        ) from None
    if expected_repository is not None and repository != expected_repository:
        raise CredentialError(
            "The workspace's primary checkout now has a different Git origin. "
            "Verify the checkout and rerun 'claude workspace bind'; no customer was selected."
        )
    identity = resolve_identity(store, profile, repository, NativeKeyring, SessionManager)
    return ClaudeContext(
        str(store.home.resolve()), profile.name, profile.account, identity.user_id,
        repository, identity.customer, configuration.model,
    ), profile


def setup(
    store: ProfileStore, profile_name: str | None, model: str,
) -> dict:
    """Validate repo/vault access, save nonsecret adapter config, and return VS Code settings to merge; no editor files are changed."""
    configuration = ClaudeConfiguration(store.get(profile_name).name, model)
    executable = verify_wrapper()
    workspace = Path.cwd()
    binding = WorkspaceStore(store).get(workspace)
    resolve_context(
        store, configuration, Path(binding.directory) if binding else workspace,
        binding.repository if binding else None,
    )
    helper_command()
    with store.locked("claude-integration"):
        atomic_write_json(store.home / "claude.json", {"version": 1, **configuration.__dict__})
    environment_variables = [
        {"name": "LLM_CREDENTIAL_MAPPING_HOME", "value": str(store.home.resolve())},
    ]
    bus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if sys.platform == "linux" and bus_address:
        environment_variables.append({"name": "DBUS_SESSION_BUS_ADDRESS", "value": bus_address})
    return {
        "claudeCode.claudeProcessWrapper": str(executable),
        "claudeCode.disableLoginPrompt": True,
        "claudeCode.environmentVariables": environment_variables,
    }


def check(store: ProfileStore, directory: Path | None = None) -> dict:
    """Resolve a launch directory's primary checkout and verify/renew its credential; return nonsecret diagnostics."""
    executable = verify_wrapper()
    helper_command()
    workspace = directory.expanduser().resolve() if directory is not None else Path.cwd()
    binding = WorkspaceStore(store).get(workspace)
    directory = Path(binding.directory) if binding else workspace
    context, profile = resolve_context(
        store, load_configuration(store), directory, binding.repository if binding else None,
    )
    return {
        "profile": context.profile, "gateway": profile.gateway, "team_id": team_id(profile),
        "user_id": context.user_id, "repository": context.repository, "customer": context.customer,
        "model": context.model, "wrapper": str(executable), "workspace_directory": str(workspace),
        "lookup_directory": str(directory), "primary_repository_mapped": binding is not None, "state": "ready",
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
    project_root: Path | None = None,
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
        if option in ("--settings", "--model", "--fallback-model") or (
            option == "--project-config-root" and project_root is not None
        ):
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
            elif option == "--project-config-root":
                forwarded.extend([option, str(project_root)])
        else:
            forwarded.append(argument)
        index += 1
    inherited = {**os.environ, **settings.get("env", {})}
    gateway = gateway_for(profile)
    base_url = gateway.endpoint(Api.ANTHROPIC_MESSAGES)
    headers = gateway.attribution_headers(Attribution(context.customer, "local", "repo bind", context.repository))
    env = claude_environment(base_url, headers, context.model, context.customer, inherited)
    overrides = {
        "ANTHROPIC_BASE_URL": base_url,
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
    settings["modelPicker"] = {
        "replaceBuiltInOptions": True,
        "options": [{
            "model": context.model,
            "label": context.model,
            "description": "Configured LiteLLM model",
        }],
    }
    command = [
        executable, *forwarded, "--model", context.model,
        "--settings", json.dumps(settings), *literal,
    ]
    return command, env


def _execute(command: list[str], env: dict[str, str], directory: Path | None = None) -> int:
    if directory is not None:
        os.chdir(directory)
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
        workspace = project_directory(forwarded, cwd)
        binding = WorkspaceStore(store).get(workspace)
        directory = Path(binding.directory) if binding else workspace
        configuration = load_configuration(store)
        try:
            context, profile = resolve_context(
                store, configuration, directory, binding.repository if binding else None,
            )
        except CredentialError as error:
            mode = "configuration-probe" if os.environ.get("CLAUDE_CODE_CONFIG_PROBE") == "1" else "session"
            raise CredentialError(
                f"{error} Claude process cwd={json.dumps(str(cwd))}; "
                f"workspace directory={json.dumps(str(workspace))}; "
                f"Git lookup directory={json.dumps(str(directory))}; mode={mode}. "
                "No fallback repository or customer was selected."
            ) from None
        command, env = wrapped_command(
            executable, forwarded, context, profile, directory if binding else None,
        )
        print(
            f"LLM credentials: profile {json.dumps(context.profile)}, customer {json.dumps(context.customer)}, "
            f"repository {json.dumps(context.repository)}, directory {json.dumps(str(directory))}. "
            "Context pinned for this process.",
            file=sys.stderr,
        )
        return _execute(command, env, directory if binding else None)
    except CredentialError as error:
        print(f"LLM Claude adapter: {error} Run 'llm-credentials claude check' on the extension host.", file=sys.stderr)
        return 1
    except OSError:
        print("LLM Claude adapter: cannot launch Claude; check executable permissions and installation.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
