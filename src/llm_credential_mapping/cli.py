import argparse
import json
import sys
from functools import cached_property
from pathlib import Path

from . import __version__
from .adapters import Api, Auth, Credential, Gateway, IntegrationContext
from .claude import check as claude_check, helper_token, setup as claude_setup
from .customers import Attribution, Tag, validate_customer
from .errors import CredentialError
from .copilot import CopilotStore, connection_key, setup as copilot_setup, validate_connection
from .copilot_bridge import check as copilot_check, serve as copilot_serve
from .editor_settings import editing, settings_file
from .profiles import Profile, ProfileStore, default_home
from .registry import AUTH, GATEWAYS, INTEGRATIONS, auth_for, gateway_for
from .repositories import RepositoryStore, current_repository
from .storage import NativeKeyring
from .workspaces import WorkspaceStore


def notify(message: str) -> None:
    print(message, file=sys.stderr)


class Context:
    """Per-invocation state: the configuration store and, lazily, the selected profile and its adapters."""

    def __init__(self, store: ProfileStore, args: argparse.Namespace) -> None:
        self.store = store
        self.args = args

    @cached_property
    def profile(self) -> Profile:
        return self.store.get(getattr(self.args, "profile", None))

    @cached_property
    def auth(self) -> Auth:
        return auth_for(self.profile, self.store, NativeKeyring())

    @cached_property
    def gateway(self) -> Gateway:
        return gateway_for(self.profile)

    def credential(self) -> Credential:
        credential = self.auth.credential()
        if credential.notice:
            notify(credential.notice)
        return credential

    def token(self) -> str:
        return self.credential().secret


def launch(agent: str, context: IntegrationContext, attribution: Attribution, arguments: list[str], model: str | None) -> int:
    return INTEGRATIONS[agent].launch(context, attribution, arguments, model)


def _visible_customers(context: Context, prefix: str | None) -> list[Tag]:
    if prefix is not None:
        validate_customer(prefix)
    customers = context.gateway.visible_customers(context.credential())
    if customers is None:
        raise CredentialError("This gateway offers no customer catalogue; pass --customer with an approved customer.")
    return [customer for customer in customers if prefix is None or customer.name.startswith(prefix)]


def _pick_customer(context: Context, prefix: str | None) -> str:
    if not sys.stdin.isatty():
        raise CredentialError("No customer selected. Use --customer TAG or bind this repository with 'repo bind'.")
    tags = _visible_customers(context, prefix)
    if not tags:
        raise CredentialError(
            "No visible customers matched. The gateway may list only customers you used before. "
            "Ask an administrator for an approved customer and pass --customer."
        )
    print("Visible customers are not an authorization list. Choose the approved customer for this session:", file=sys.stderr)
    for index, tag in enumerate(tags, 1):
        description = f" - {json.dumps(tag.description)}" if tag.description else ""
        print(f"{index}. {json.dumps(tag.name)}{description}", file=sys.stderr)
    print("Customer (number or exact name): ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip()
    if len(answer) > 256:
        raise CredentialError("Invalid customer selection; use a displayed name or number.")
    if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(tags):
        return validate_customer(tags[int(answer) - 1].name)
    if any(tag.name == answer for tag in tags):
        return validate_customer(answer)
    raise CredentialError("Invalid customer selection; choose a displayed customer or pass --customer explicitly.")


# Profiles and sessions


def profile_add(context: Context) -> None:
    args = context.args
    settings = {"team_id": args.team_id} if args.team_id is not None else {}
    profile = Profile(
        args.name, args.gateway, settings, args.gateway_type, args.auth, args.allow_insecure_localhost,
    )
    context.store.add(profile)
    print(f"Added profile '{profile.name}' ({profile.gateway_type} gateway, {profile.auth_type} sign-in).")


def profile_list(context: Context) -> None:
    active, profiles = context.store.list()
    for profile in profiles:
        marker = "*" if profile.name == active else " "
        print(f"{marker} {profile.name}\t{profile.gateway}\t{profile.auth_type} {json.dumps(profile.auth, sort_keys=True)}")


def profile_show(context: Context) -> None:
    profile = context.store.get(context.args.name)
    gateway = gateway_for(profile)
    endpoints = {}
    for api in Api:
        try:
            endpoints[api.value] = gateway.endpoint(api)
        except CredentialError:
            pass
    value = {
        "name": profile.name,
        "gateway": {"type": profile.gateway_type, "url": profile.gateway},
        "auth": {"type": profile.auth_type, **profile.auth},
        "endpoints": endpoints,
    }
    if context.args.json:
        print(json.dumps(value))
        return
    print(json.dumps(value, indent=2))


def profile_use(context: Context) -> None:
    context.store.select(context.args.name)
    print(f"Selected profile '{context.args.name}'. Running agents are unchanged.")


def login(context: Context) -> None:
    timeout = context.args.timeout
    if not 1 <= timeout <= 900:
        raise CredentialError("--timeout must be between 1 and 900 seconds.")
    credential = context.auth.login(notify, no_browser=context.args.no_browser, timeout=timeout)
    print(f"Signed in as {credential.user} for '{context.profile.name}'.")


def token(context: Context) -> None:
    credential = context.credential()
    if not context.args.json:
        print(credential.secret)
        return
    print(json.dumps({
        "access_token": credential.secret,
        "expires_at": credential.expires_at,
        "profile": context.profile.name,
        "user": credential.user,
    }))


def status(context: Context) -> None:
    print(json.dumps(context.auth.status(), indent=2))


def logout(context: Context) -> None:
    context.auth.logout()
    print(f"Logged out of '{context.profile.name}'. Previously issued access tokens expire naturally.")


# Customers, repositories and agents


def tags_list(context: Context) -> None:
    tags = _visible_customers(context, context.args.prefix)
    if context.args.json:
        print(json.dumps([{"name": tag.name, "description": tag.description} for tag in tags], indent=2))
        return
    for tag in tags:
        print(f"{json.dumps(tag.name)}\t{json.dumps(tag.description)}")
    notify(f"{len(tags)} visible customer(s). Visibility is not customer authorization.")


def repo_list(context: Context) -> None:
    print(json.dumps(RepositoryStore(context.store).list(context.profile.gateway), indent=2))


def repo_bind(context: Context) -> None:
    args = context.args
    profile = context.profile
    repository = current_repository(args.directory.expanduser().resolve() if args.directory else None)
    customer = validate_customer(args.customer) if args.customer is not None else _pick_customer(
        context, args.tag_prefix,
    )
    RepositoryStore(context.store).bind(profile.gateway, repository, customer, replace=True)
    print(f"Bound {json.dumps(repository)} to {json.dumps(customer)}. Running agents are unchanged.")


def repo_unbind(context: Context) -> None:
    args = context.args
    repository = current_repository(args.directory.expanduser().resolve() if args.directory else None)
    RepositoryStore(context.store).unbind(context.profile.gateway, repository)
    print(f"Removed binding for {json.dumps(repository)}. Running agents are unchanged.")


def run(context: Context) -> int:
    args = context.args
    profile = context.profile
    if args.customer is not None:
        attribution = Attribution(validate_customer(args.customer), "explicit", "--customer")
    else:
        repository = current_repository()
        repositories = RepositoryStore(context.store)
        customer = repositories.get(profile.gateway, repository)
        if customer is None:
            customer = _pick_customer(context, args.tag_prefix)
            repositories.bind(profile.gateway, repository, customer)
            notify("Saved this repository's customer default.")
        attribution = Attribution(customer, "local", f"repo bind for {repository}", repository)
    arguments = args.agent_args
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    notify(f"Customer {json.dumps(attribution.customer)} is pinned for this process (profile {profile.name}).")
    integration = IntegrationContext(profile, context.gateway, context.token())
    return launch(args.agent, integration, attribution, arguments, args.model)


# Official Claude Code VS Code extension


def claude_workspace(context: Context) -> None:
    args = context.args
    workspaces = WorkspaceStore(context.store)
    if args.workspace_action == "list":
        print(json.dumps([binding.__dict__ for binding in workspaces.list()], indent=2))
        return
    workspace = args.workspace if args.workspace is not None else Path.cwd()
    if args.workspace_action == "bind":
        print(json.dumps(workspaces.bind(workspace, args.repository).__dict__, indent=2))
    else:
        workspaces.unbind(workspace)
        print("Removed this workspace's primary repository mapping.")
    notify(
        "Reload the VS Code window before using the changed selection. "
        "Running processes are unchanged; editor tabs do not change customer tags."
    )


def claude_setup_command(context: Context) -> None:
    args = context.args
    if args.settings_file is not None and not args.apply:
        raise CredentialError("--settings-file requires --apply; no settings were edited.")
    if not args.apply:
        print(json.dumps(claude_setup(context.store, args.profile, args.model), indent=2))
        notify(
            "Merge this snippet into VS Code User Settings (JSON), or Remote User Settings in a container. "
            "Keep unrelated environmentVariables entries, but remove old gateway credentials/routing overrides. "
            "Reload the window and start a new Claude conversation. No editor settings were edited."
        )
        return
    target = settings_file(args.settings_file)
    with editing(target) as editor:
        generated = claude_setup(context.store, args.profile, args.model)
        try:
            editor.apply(generated)
        except CredentialError as error:
            raise CredentialError(
                f"Claude's CLI configuration was saved, but editor settings were not applied: {error}"
            ) from None
    print(json.dumps(generated, indent=2))
    notify(
        f"Applied Claude bootstrap settings to {json.dumps(str(target))}. "
        "Unrelated settings and environment entries were preserved. "
        "Reload the window and start a new Claude conversation."
    )


def claude_disable(context: Context) -> None:
    target = settings_file(context.args.settings_file)
    with editing(target) as editor:
        changed = editor.disable()
    print(
        "Disconnected the Claude Code extension from LiteLLM."
        if changed else "No LiteLLM Claude bootstrap settings found; nothing was changed."
    )
    notify(
        f"Settings target: {json.dumps(str(target))}. "
        "LiteLLM logins, profiles and repository/workspace mappings were retained. "
        "D-Bus and unrelated settings were preserved. "
        "Reload the window, start a new conversation and use Claude's normal sign-in if prompted. "
        "Other gateway settings or editor policies are not removed."
    )


def claude_check_command(context: Context) -> None:
    print(json.dumps(claude_check(context.store, context.args.directory), indent=2))


def claude_token(context: Context) -> None:
    print(helper_token())


# Copilot Chat foreground bridge


def copilot_setup_command(context: Context) -> None:
    args = context.args
    connection = copilot_setup(
        context.store, args.profile, args.model, args.directory, port=args.port,
        max_input_tokens=args.max_input_tokens, max_output_tokens=args.max_output_tokens, vision=args.vision,
    )
    print(json.dumps(connection.model_settings(), indent=2))
    notify(
        f"Saved connection {connection.id}. Group name: {json.dumps(connection.group_name)}. "
        "In Chat: Manage Language Models, add Custom Endpoint and enter the LOCAL key from 'copilot key'. "
        "Choose Chat Completions. Merge only these apiType/models fields into the new group; "
        "KEEP its VS Code-generated apiKey secret reference, name and vendor. "
        "No editor files were changed. Run 'copilot serve' in this extension-host terminal, "
        "then select this repository/customer model explicitly. Token limits are conservative defaults; "
        "configure them to match the gateway model. No automatic active-workspace selection."
    )


def copilot_list(context: Context) -> None:
    print(json.dumps([
        {"connection": connection.id, **connection.__dict__}
        for connection in CopilotStore(context.store).list()
    ], indent=2))


def copilot_key(context: Context) -> None:
    connection = CopilotStore(context.store).select(context.args.connection)
    _, manager = validate_connection(context.store, connection)
    print(connection_key(connection, manager.secrets))
    notify(
        "This is a LOCAL bridge credential, not a LiteLLM access/refresh token. "
        "Enter it only in this connection's native VS Code API key prompt; do not share or commit it."
    )


def copilot_serve_command(context: Context) -> None:
    copilot_serve(context.store, context.args.connection, notify)


def copilot_check_command(context: Context) -> None:
    print(json.dumps(copilot_check(context.store, context.args.connection), indent=2))


def copilot_disable(context: Context) -> None:
    connection = CopilotStore(context.store).disable(context.args.connection)
    print(f"Disabled Copilot connection {connection.id}.")
    notify(
        "New requests are blocked and its foreground bridge stops; in-flight requests may be interrupted. "
        "Check the outcome before retrying interrupted generations or tool calls. "
        "Select a normal GitHub Copilot model in the picker and start a new conversation. "
        "Native UI registration, local key, LiteLLM logins and mappings were retained; "
        "remove the group manually in Manage Language Models if desired. Rerun setup/serve to reconnect."
    )


def _profile_option(command: argparse.ArgumentParser) -> None:
    command.add_argument("--profile", help="Authentication profile; otherwise the selected default.")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="llm-credentials",
        description="Sign in to an LLM gateway and launch agents with session-local customer attribution.",
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", type=Path, help="Nonsecret configuration directory.")
    commands = root.add_subparsers(dest="command", required=True)

    profile = commands.add_parser("profile", help="Manage gateway and sign-in profiles.")
    profile_actions = profile.add_subparsers(dest="action", required=True)
    add = profile_actions.add_parser("add")
    add.add_argument("name")
    add.add_argument("--gateway", required=True, help="Trusted gateway public root URL, without an API path.")
    add.add_argument("--gateway-type", choices=sorted(GATEWAYS), default="litellm", help="Gateway kind (default: litellm).")
    add.add_argument("--auth", choices=sorted(AUTH), default="litellm-sso", help="Sign-in kind (default: litellm-sso).")
    add.add_argument("--team-id", help="litellm-sso: exact LiteLLM team ID chosen at browser consent, not its display name.")
    add.add_argument("--allow-insecure-localhost", action="store_true", help="Development only.")
    add.set_defaults(handler=profile_add)
    profile_actions.add_parser("list").set_defaults(handler=profile_list)
    show = profile_actions.add_parser("show", help="Show a profile and its gateway endpoints per API, without credentials.")
    show.add_argument("name", nargs="?", help="Profile name; otherwise the selected default.")
    show.add_argument("--json", action="store_true", help="Print one line of JSON for other tools.")
    show.set_defaults(handler=profile_show)
    use = profile_actions.add_parser("use")
    use.add_argument("name")
    use.set_defaults(handler=profile_use)

    command = commands.add_parser("login", help="Sign in with the profile's sign-in kind, e.g. SSO in a browser.")
    _profile_option(command)
    command.add_argument("--no-browser", action="store_true", help="Print the sign-in URL instead of opening it.")
    command.add_argument("--timeout", type=int, default=180, help="Browser callback wait in seconds.")
    command.set_defaults(handler=login)
    command = commands.add_parser("token", help="Print a current access token for credential helpers.")
    _profile_option(command)
    command.add_argument("--json", action="store_true", help="Print the token with its expiry and nonsecret context.")
    command.set_defaults(handler=token)
    for name, handler, text in (
        ("status", status, "Show the sign-in state without printing credentials."),
        ("logout", logout, "Revoke the sign-in where possible and delete the local credential."),
    ):
        command = commands.add_parser(name, help=text)
        _profile_option(command)
        command.set_defaults(handler=handler)

    tags = commands.add_parser("tags", help="Discover customers the gateway lists; this is not an authorization list.")
    tag_list = tags.add_subparsers(dest="action", required=True).add_parser("list")
    _profile_option(tag_list)
    tag_list.add_argument("--prefix", help="Only display customers starting with this exact prefix.")
    tag_list.add_argument("--json", action="store_true")
    tag_list.set_defaults(handler=tags_list)

    repo = commands.add_parser("repo", help="Manage repository customer defaults.")
    repo_actions = repo.add_subparsers(dest="action", required=True)
    for action, handler in (("bind", repo_bind), ("list", repo_list), ("unbind", repo_unbind)):
        command = repo_actions.add_parser(action)
        _profile_option(command)
        if action != "list":
            command.add_argument("--directory", type=Path, help="Repository directory; otherwise current directory.")
        if action == "bind":
            command.add_argument("--customer", help="Exact approved customer; otherwise choose from visible customers.")
            command.add_argument("--tag-prefix", help="Filter the customer picker by this exact prefix.")
        command.set_defaults(handler=handler)

    command = commands.add_parser("run", help="Launch an agent with the repository's customer attribution.")
    _profile_option(command)
    command.add_argument("--customer", help="Exact customer for this process only; does not change the repo default.")
    command.add_argument("--tag-prefix", help="Filter the customer picker for an unbound repository.")
    command.add_argument("--model", help="Gateway model alias; required for OpenCode.")
    command.add_argument("agent", choices=sorted(INTEGRATIONS))
    command.add_argument("agent_args", nargs=argparse.REMAINDER)
    command.set_defaults(handler=run)

    claude = commands.add_parser("claude", help="Integrate the official Claude Code VS Code extension.")
    claude_actions = claude.add_subparsers(dest="action", required=True)
    setup = claude_actions.add_parser("setup", help="Validate this repository and print or apply editor settings.")
    _profile_option(setup)
    setup.add_argument("--model", required=True, help="LiteLLM alias pinned for extension model requests.")
    setup.add_argument("--apply", action="store_true", help="Apply bootstrap settings to VS Code User/Remote Settings.")
    setup.add_argument("--settings-file", type=Path, help="Explicit User/Remote settings.json; requires --apply.")
    setup.set_defaults(handler=claude_setup_command)
    disable = claude_actions.add_parser("disable", help="Disconnect the extension from LiteLLM; keep logins and mappings.")
    disable.add_argument("--settings-file", type=Path, help="Explicit User/Remote settings.json; otherwise detect the editor.")
    disable.set_defaults(handler=claude_disable)
    check = claude_actions.add_parser("check", help="Check repo binding and vault access without printing credentials.")
    check.add_argument("--directory", type=Path, help="Launch/workspace directory to diagnose; otherwise current directory.")
    check.set_defaults(handler=claude_check_command)
    claude_actions.add_parser(
        "token", help="Internal apiKeyHelper: return the pinned session credential only.",
    ).set_defaults(handler=claude_token)
    workspace = claude_actions.add_parser("workspace", help="Select an explicit primary checkout for a launch workspace.")
    workspace_actions = workspace.add_subparsers(dest="workspace_action", required=True)
    for action in ("bind", "list", "unbind"):
        command = workspace_actions.add_parser(action)
        if action != "list":
            command.add_argument("--workspace", type=Path, help="Claude's launch directory; otherwise current directory.")
        if action == "bind":
            command.add_argument("--repository", type=Path, required=True, help="Primary Git checkout's local directory.")
        command.set_defaults(handler=claude_workspace)

    copilot = commands.add_parser("copilot", help="Prototype: connect Copilot Chat through a pinned foreground bridge.")
    copilot_actions = copilot.add_subparsers(dest="action", required=True)
    copilot_setup_parser = copilot_actions.add_parser("setup", help="Save a bound connection and print fields for native UI setup.")
    _profile_option(copilot_setup_parser)
    copilot_setup_parser.add_argument("--model", required=True, help="Exact LiteLLM model alias; agents require tool calling.")
    copilot_setup_parser.add_argument("--directory", type=Path, help="Git checkout to pin; otherwise current directory.")
    copilot_setup_parser.add_argument("--port", type=int, default=8743, help="Private loopback port; default 8743.")
    copilot_setup_parser.add_argument("--max-input-tokens", type=int, default=32000, help="Conservative advertised input limit.")
    copilot_setup_parser.add_argument("--max-output-tokens", type=int, default=4096, help="Advertised and enforced output limit.")
    copilot_setup_parser.add_argument("--vision", action="store_true", help="Declare image support only if the model supports it.")
    copilot_setup_parser.set_defaults(handler=copilot_setup_command)
    copilot_actions.add_parser("list", help="List saved nonsecret connections.").set_defaults(handler=copilot_list)
    for action, handler, text in (
        ("key", copilot_key, "Print only the local connection key for VS Code's API key prompt; treat it as a secret."),
        ("serve", copilot_serve_command, "Run the foreground bridge in this extension-host terminal; Ctrl+C stops it."),
        ("disable", copilot_disable, "Block the saved connection and stop its foreground bridge; retain vault logins and UI registration."),
        ("check", copilot_check_command, "Verify bridge readiness without printing either local or gateway credentials."),
    ):
        command = copilot_actions.add_parser(action, help=text)
        command.add_argument("--connection", help="Exact ID from copilot list; optional only when one connection exists.")
        command.set_defaults(handler=handler)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        store = ProfileStore(args.home.expanduser().resolve() if args.home else default_home())
        return args.handler(Context(store, args)) or 0
    except CredentialError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
