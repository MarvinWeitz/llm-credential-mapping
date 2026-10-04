import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .agents import launch
from .claude import check as claude_check, helper_token, setup as claude_setup
from .errors import CredentialError
from .copilot import CopilotConnection, CopilotStore, connection_key, setup as copilot_setup, validate_connection
from .copilot_bridge import check as copilot_check, serve as copilot_serve
from .copilot_models import ModelsDocument, models_file
from .editor_settings import editing, editing_document, settings_file
from .profiles import Profile, ProfileStore, default_home
from .repositories import RepositoryStore, current_repository
from .sessions import SessionManager
from .storage import NativeKeyring
from .tags import list_tags, validate_tag
from .workspaces import WorkspaceStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="llm-credentials",
        description="Sign in to a LiteLLM business unit and launch agents with session-local customer tags.",
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", type=Path, help="Nonsecret configuration directory.")
    commands = root.add_subparsers(dest="command", required=True)
    claude = commands.add_parser("claude", help="Integrate the official Claude Code VS Code extension.")
    claude_actions = claude.add_subparsers(dest="action", required=True)
    setup = claude_actions.add_parser("setup", help="Validate this repository and print or apply editor settings.")
    setup.add_argument("--project", "--profile", dest="project")
    setup.add_argument("--model", required=True, help="LiteLLM alias pinned for extension model requests.")
    setup.add_argument("--apply", action="store_true", help="Apply bootstrap settings to VS Code User/Remote Settings.")
    setup.add_argument("--settings-file", type=Path, help="Explicit User/Remote settings.json; requires --apply.")
    disable = claude_actions.add_parser("disable", help="Disconnect the extension from LiteLLM; keep logins and mappings.")
    disable.add_argument("--settings-file", type=Path, help="Explicit User/Remote settings.json; otherwise detect the editor.")
    check = claude_actions.add_parser("check", help="Check repo binding and vault access without printing credentials.")
    check.add_argument("--directory", type=Path, help="Launch/workspace directory to diagnose; otherwise current directory.")
    claude_actions.add_parser("token", help="Internal apiKeyHelper: return the pinned session credential only.")
    workspace = claude_actions.add_parser("workspace", help="Select an explicit primary checkout for a launch workspace.")
    workspace_actions = workspace.add_subparsers(dest="workspace_action", required=True)
    for action in ("bind", "list", "unbind"):
        cmd = workspace_actions.add_parser(action)
        if action != "list":
            cmd.add_argument("--workspace", type=Path, help="Claude's launch directory; otherwise current directory.")
        if action == "bind":
            cmd.add_argument("--repository", type=Path, required=True, help="Primary Git checkout's local directory.")
    copilot = commands.add_parser("copilot", help="Prototype: connect Copilot Chat through a pinned foreground bridge.")
    copilot_actions = copilot.add_subparsers(dest="action", required=True)
    copilot_setup_parser = copilot_actions.add_parser("setup", help="Save a bound connection and print or apply native model settings.")
    copilot_setup_parser.add_argument("--project", "--profile", dest="project")
    copilot_setup_parser.add_argument("--model", required=True, help="Exact LiteLLM model alias; agents require tool calling.")
    copilot_setup_parser.add_argument("--directory", type=Path, help="Git checkout to pin; otherwise current directory.")
    copilot_setup_parser.add_argument("--port", type=int, default=8743, help="Private loopback port; default 8743.")
    copilot_setup_parser.add_argument("--max-input-tokens", type=int, default=32000, help="Conservative advertised input limit.")
    copilot_setup_parser.add_argument("--max-output-tokens", type=int, default=4096, help="Advertised and enforced output limit.")
    copilot_setup_parser.add_argument("--vision", action="store_true", help="Declare image support only if the model supports it.")
    copilot_setup_parser.add_argument("--apply", action="store_true", help="Update an existing native Custom Endpoint group.")
    copilot_setup_parser.add_argument(
        "--models-file", type=Path, help="Desktop profile chatLanguageModels.json; requires --apply and native key registration.",
    )
    copilot_actions.add_parser("list", help="List saved nonsecret connections.")
    for action in ("key", "serve", "disable", "check"):
        command = copilot_actions.add_parser(action, help={
            "key": "Print only the local connection key for VS Code's API key prompt; treat it as a secret.",
            "serve": "Run the foreground bridge in this extension-host terminal; Ctrl+C stops it.",
            "disable": "Block the saved connection and stop its foreground bridge; retain vault logins and UI registration.",
            "check": "Verify bridge readiness without printing either local or gateway credentials.",
        }[action])
        command.add_argument("--connection", help="Exact ID from copilot list; optional only when one connection exists.")
    project = commands.add_parser("project", aliases=["profile"], help="Manage gateway/business-unit profiles.")
    actions = project.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add")
    add.add_argument("name")
    add.add_argument("--gateway", required=True, help="Trusted LiteLLM public origin, not /v1.")
    add.add_argument("--team-id", required=True, help="Exact LiteLLM team ID, not its display name.")
    add.add_argument("--allow-insecure-localhost", action="store_true", help="Development only.")
    actions.add_parser("list")
    use = actions.add_parser("use")
    use.add_argument("name")
    tags = commands.add_parser("tags", help="Discover visible gateway tags; this is not an authorization list.")
    tag_actions = tags.add_subparsers(dest="action", required=True)
    tag_list = tag_actions.add_parser("list")
    tag_list.add_argument("--project", "--profile", dest="project")
    tag_list.add_argument("--prefix", help="Only display tags starting with this exact prefix.")
    tag_list.add_argument("--json", action="store_true")
    repo = commands.add_parser("repo", help="Manage repository customer defaults.")
    repo_actions = repo.add_subparsers(dest="action", required=True)
    for action in ("bind", "list", "unbind"):
        cmd = repo_actions.add_parser(action)
        cmd.add_argument("--project", "--profile", dest="project")
        if action != "list":
            cmd.add_argument("--directory", type=Path, help="Repository directory; otherwise current directory.")
        if action == "bind":
            cmd.add_argument("--customer", help="Exact approved customer tag; otherwise choose from visible tags.")
            cmd.add_argument("--tag-prefix", help="Filter the tag picker by this exact prefix.")
    for name in ("login", "token", "status", "logout", "run"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--project", "--profile", dest="project", help="Explicit profile; otherwise the selected default.")
        if name == "login":
            cmd.add_argument("--no-browser", action="store_true")
            cmd.add_argument("--timeout", type=int, default=180, help="Browser callback wait in seconds.")
        if name == "run":
            cmd.add_argument("--customer", help="Exact customer tag for this process only; does not change the repo default.")
            cmd.add_argument("--tag-prefix", help="Filter the tag picker for an unbound repository.")
            cmd.add_argument("--model", help="LiteLLM model alias; required for OpenCode.")
            cmd.add_argument("agent", choices=("claude", "codex", "opencode"))
            cmd.add_argument("agent_args", nargs=argparse.REMAINDER)
    return root


def _pick_customer(profile: Profile, token: Callable[[], str], prefix: str | None) -> str:
    if not sys.stdin.isatty():
        raise CredentialError("No customer selected. Use --customer TAG or bind this repository with 'repo bind'.")
    tags = list_tags(profile, token(), prefix)
    if not tags:
        raise CredentialError(
            "No visible tags matched. /tag/list may contain only previously used tags. "
            "Ask an administrator for an approved customer tag and pass --customer."
        )
    print("Visible tags are not an authorization list. Choose the approved customer for this session:", file=sys.stderr)
    for index, tag in enumerate(tags, 1):
        description = f" - {json.dumps(tag.description)}" if tag.description else ""
        print(f"{index}. {json.dumps(tag.name)}{description}", file=sys.stderr)
    print("Customer tag (number or exact name): ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip()
    if len(answer) > 256:
        raise CredentialError("Invalid customer selection; use a displayed tag name or number.")
    if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(tags):
        return validate_tag(tags[int(answer) - 1].name)
    if any(tag.name == answer for tag in tags):
        return validate_tag(answer)
    raise CredentialError("Invalid customer selection; choose a displayed tag or pass --customer explicitly.")


def _setup_copilot(store: ProfileStore, args: argparse.Namespace) -> CopilotConnection:
    return copilot_setup(
        store, args.project, args.model, args.directory, port=args.port,
        max_input_tokens=args.max_input_tokens, max_output_tokens=args.max_output_tokens, vision=args.vision,
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        store = ProfileStore(args.home.expanduser().resolve() if args.home else default_home())
        if args.command == "copilot":
            if args.action == "setup":
                if args.models_file is not None and not args.apply:
                    raise CredentialError("--models-file requires --apply; no model settings were edited.")
                if args.apply:
                    target = models_file(args.models_file)
                    with editing_document(target, ModelsDocument) as editor:
                        connection = _setup_copilot(store, args)
                        try:
                            changed = editor.write(editor.document.merge(connection))
                        except CredentialError as error:
                            raise CredentialError(
                                f"Copilot connection {connection.id} was saved, but model settings were not applied: {error}"
                            ) from None
                    print(
                        f"{'Applied' if changed else 'Already up to date:'} Copilot model settings in {json.dumps(str(target))}. "
                        "Native apiKey reference, other provider groups/models and custom fields were preserved. "
                        "If the model does not appear, reload the VS Code window.",
                        file=sys.stderr,
                    )
                else:
                    connection = _setup_copilot(store, args)
                print(json.dumps(connection.model_settings(), indent=2))
                if not args.apply:
                    print(
                        "In Chat: Manage Language Models, add Custom Endpoint and enter the LOCAL key from 'copilot key'. "
                        "Choose Chat Completions. Merge only these apiType/models fields into the new group; "
                        "KEEP its VS Code-generated apiKey secret reference, name and vendor. "
                        "Or rerun setup with --apply --models-file PATH after native registration. No editor files were changed.",
                        file=sys.stderr,
                    )
                print(
                    f"Saved connection {connection.id}. Group name: {json.dumps(connection.group_name)}. "
                    "Run 'copilot serve' in this extension-host terminal, "
                    "then select this repository/customer model explicitly. Token limits are conservative defaults; "
                    "configure them to match the gateway model. No automatic active-workspace selection.",
                    file=sys.stderr,
                )
            elif args.action == "list":
                print(json.dumps([
                    {"connection": connection.id, **connection.__dict__}
                    for connection in CopilotStore(store).list()
                ], indent=2))
            elif args.action == "key":
                connection = CopilotStore(store).select(args.connection)
                _, manager = validate_connection(store, connection)
                print(connection_key(connection, manager.secrets))
                print(
                    "This is a LOCAL bridge credential, not a LiteLLM access/refresh token. "
                    "Enter it only in this connection's native VS Code API key prompt; do not share or commit it.",
                    file=sys.stderr,
                )
            elif args.action == "serve":
                copilot_serve(store, args.connection, lambda message: print(message, file=sys.stderr))
            elif args.action == "check":
                print(json.dumps(copilot_check(store, args.connection), indent=2))
            else:
                connection = CopilotStore(store).disable(args.connection)
                print(f"Disabled Copilot connection {connection.id}.")
                print(
                    "New requests are blocked and its foreground bridge stops; in-flight requests may be interrupted. "
                    "Check the outcome before retrying interrupted generations or tool calls. "
                    "Select a normal GitHub Copilot model in the picker and start a new conversation. "
                    "Native UI registration, local key, LiteLLM logins and mappings were retained; "
                    "remove the group manually in Manage Language Models if desired. Rerun setup/serve to reconnect.",
                    file=sys.stderr,
                )
            return 0
        if args.command == "claude":
            if args.action == "workspace":
                workspaces = WorkspaceStore(store)
                if args.workspace_action == "list":
                    print(json.dumps([binding.__dict__ for binding in workspaces.list()], indent=2))
                else:
                    workspace = args.workspace if args.workspace is not None else Path.cwd()
                    if args.workspace_action == "bind":
                        print(json.dumps(workspaces.bind(workspace, args.repository).__dict__, indent=2))
                    else:
                        workspaces.unbind(workspace)
                        print("Removed this workspace's primary repository mapping.")
                    print(
                        "Reload the VS Code window before using the changed selection. "
                        "Running processes are unchanged; editor tabs do not change customer tags.",
                        file=sys.stderr,
                    )
            elif args.action == "setup":
                if args.settings_file is not None and not args.apply:
                    raise CredentialError("--settings-file requires --apply; no settings were edited.")
                if args.apply:
                    target = settings_file(args.settings_file)
                    with editing(target) as editor:
                        generated = claude_setup(store, args.project, args.model)
                        try:
                            editor.apply(generated)
                        except CredentialError as error:
                            raise CredentialError(
                                f"Claude's CLI configuration was saved, but editor settings were not applied: {error}"
                            ) from None
                    print(json.dumps(generated, indent=2))
                    print(
                        f"Applied Claude bootstrap settings to {json.dumps(str(target))}. "
                        "Unrelated settings and environment entries were preserved. "
                        "Reload the window and start a new Claude conversation.",
                        file=sys.stderr,
                    )
                else:
                    print(json.dumps(claude_setup(store, args.project, args.model), indent=2))
                    print(
                        "Merge this snippet into VS Code User Settings (JSON), or Remote User Settings in a container. "
                        "Keep unrelated environmentVariables entries, but remove old gateway credentials/routing overrides. "
                        "Reload the window and start a new Claude conversation. No editor settings were edited.",
                        file=sys.stderr,
                    )
            elif args.action == "disable":
                target = settings_file(args.settings_file)
                with editing(target) as editor:
                    changed = editor.disable()
                print(
                    "Disconnected the Claude Code extension from LiteLLM."
                    if changed else "No LiteLLM Claude bootstrap settings found; nothing was changed."
                )
                print(
                    f"Settings target: {json.dumps(str(target))}. "
                    "LiteLLM logins, profiles and repository/workspace mappings were retained. "
                    "D-Bus and unrelated settings were preserved. "
                    "Reload the window, start a new conversation and use Claude's normal sign-in if prompted. "
                    "Other gateway settings or editor policies are not removed.",
                    file=sys.stderr,
                )
            elif args.action == "check":
                print(json.dumps(claude_check(store, args.directory), indent=2))
            else:
                print(helper_token())
            return 0
        if args.command in ("project", "profile"):
            label = "project" if args.command == "project" else "profile"
            if args.action == "add":
                profile = Profile(args.name, args.gateway, args.team_id, args.allow_insecure_localhost)
                store.add(profile)
                print(f"Added {label} '{profile.name}' (business-unit team {profile.team_id}).")
            elif args.action == "use":
                store.select(args.name)
                print(f"Selected {label} '{args.name}'. Running agents are unchanged.")
            else:
                active, profiles = store.list()
                for profile in profiles:
                    marker = "*" if profile.name == active else " "
                    print(f"{marker} {profile.name}\t{profile.gateway}\t{profile.team_id}")
            return 0
        profile = store.get(args.project)
        manager: SessionManager | None = None

        def credentials() -> SessionManager:
            nonlocal manager
            if manager is None:
                manager = SessionManager(store, NativeKeyring())
            return manager

        token = lambda: credentials().token(profile)
        notify = lambda message: print(message, file=sys.stderr)
        if args.command == "tags":
            tags = list_tags(profile, token(), args.prefix)
            if args.json:
                print(json.dumps([{"name": tag.name, "description": tag.description} for tag in tags], indent=2))
            else:
                for tag in tags:
                    print(f"{json.dumps(tag.name)}\t{json.dumps(tag.description)}")
                notify(f"{len(tags)} visible tag(s). Visibility is not customer authorization.")
            return 0
        if args.command == "repo":
            repositories = RepositoryStore(store)
            if args.action == "list":
                print(json.dumps(repositories.list(profile.gateway), indent=2))
            else:
                directory = args.directory.expanduser().resolve() if args.directory else None
                repository = current_repository(directory)
                if args.action == "bind":
                    customer = validate_tag(args.customer) if args.customer is not None else _pick_customer(
                        profile, token, args.tag_prefix,
                    )
                    repositories.bind(profile.gateway, repository, customer, replace=True)
                    print(f"Bound {json.dumps(repository)} to {json.dumps(customer)}. Running agents are unchanged.")
                else:
                    repositories.unbind(profile.gateway, repository)
                    print(f"Removed binding for {json.dumps(repository)}. Running agents are unchanged.")
            return 0
        if args.command == "login":
            if not 1 <= args.timeout <= 900:
                raise CredentialError("--timeout must be between 1 and 900 seconds.")
            session = credentials().login(profile, notify, no_browser=args.no_browser, timeout=args.timeout)
            print(f"Signed in as {session.user_id} for '{profile.name}' (team {session.team_id}).")
        elif args.command == "token":
            print(token())
        elif args.command == "status":
            print(json.dumps(credentials().status(profile), indent=2))
        elif args.command == "logout":
            credentials().logout(profile)
            print(f"Logged out of '{profile.name}'. Previously issued access tokens expire naturally.")
        elif args.command == "run":
            if args.customer is not None:
                customer = validate_tag(args.customer)
            else:
                repository = current_repository()
                repositories = RepositoryStore(store)
                customer = repositories.get(profile.gateway, repository)
                if customer is None:
                    customer = _pick_customer(profile, token, args.tag_prefix)
                    repositories.bind(profile.gateway, repository, customer)
                    notify("Saved this repository's customer default.")
            arguments = args.agent_args
            if arguments and arguments[0] == "--":
                arguments = arguments[1:]
            notify(f"Customer {json.dumps(customer)} is pinned for this process (business unit {profile.team_id}).")
            return launch(args.agent, profile, token(), arguments, args.model, customer)
        return 0
    except CredentialError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
