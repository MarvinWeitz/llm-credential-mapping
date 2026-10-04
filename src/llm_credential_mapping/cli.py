import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .agents import launch
from .claude import check as claude_check, helper_token, setup as claude_setup
from .errors import CredentialError
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
    setup = claude_actions.add_parser("setup", help="Validate this repository and print editor settings to merge.")
    setup.add_argument("--project", "--profile", dest="project")
    setup.add_argument("--model", required=True, help="LiteLLM alias pinned for extension model requests.")
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


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        store = ProfileStore(args.home.expanduser().resolve() if args.home else default_home())
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
                print(json.dumps(claude_setup(store, args.project, args.model), indent=2))
                print(
                    "Merge this snippet into VS Code User Settings (JSON), or Remote User Settings in a container. "
                    "Keep unrelated environmentVariables entries, but remove old gateway credentials/routing overrides. "
                    "Reload the window and start a new Claude conversation. No editor settings were edited.",
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
