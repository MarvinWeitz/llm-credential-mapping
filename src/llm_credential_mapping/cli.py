import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .agents import launch
from .errors import CredentialError
from .profiles import Profile, ProfileStore, default_home
from .sessions import SessionManager
from .storage import NativeKeyring


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="llm-credentials",
        description="Manually select LiteLLM projects, sign in with SSO and launch CLI agents.",
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", type=Path, help="Nonsecret configuration directory.")
    commands = root.add_subparsers(dest="command", required=True)
    project = commands.add_parser("project", help="Manage named project/team mappings.")
    actions = project.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add")
    add.add_argument("name")
    add.add_argument("--gateway", required=True, help="Trusted LiteLLM public origin, not /v1.")
    add.add_argument("--team-id", required=True, help="Exact LiteLLM team ID, not its display name.")
    add.add_argument("--allow-insecure-localhost", action="store_true", help="Development only.")
    listing = actions.add_parser("list")
    listing.add_argument("--json", action="store_true", help="Versioned, nonsecret project metadata.")
    use = actions.add_parser("use")
    use.add_argument("name")
    for name in ("login", "token", "status", "logout", "run"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--project", help="Explicit profile; otherwise use the selected default.")
        if name == "login":
            cmd.add_argument("--no-browser", action="store_true")
            cmd.add_argument("--timeout", type=int, default=180, help="Browser callback wait in seconds.")
        if name == "run":
            cmd.add_argument("--model", help="LiteLLM model alias; required for OpenCode.")
            cmd.add_argument("agent", choices=("claude", "codex", "opencode"))
            cmd.add_argument("agent_args", nargs=argparse.REMAINDER)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        store = ProfileStore(args.home.expanduser().resolve() if args.home else default_home())
        if args.command == "project":
            if args.action == "add":
                profile = Profile(args.name, args.gateway, args.team_id, args.allow_insecure_localhost)
                store.add(profile)
                print(f"Added project '{profile.name}' (team {profile.team_id}).")
            elif args.action == "use":
                store.select(args.name)
                print(f"Selected project '{args.name}'. Running agents are unchanged.")
            else:
                active, profiles = store.list()
                if args.json:
                    print(json.dumps({
                        "version": 1,
                        "active": active,
                        "projects": [
                            {
                                "name": profile.name,
                                "gateway": profile.gateway,
                                "team_id": profile.team_id,
                                "allow_http": profile.allow_http,
                            }
                            for profile in profiles
                        ],
                    }, indent=2))
                else:
                    for profile in profiles:
                        marker = "*" if profile.name == active else " "
                        print(f"{marker} {profile.name}\t{profile.gateway}\t{profile.team_id}")
            return 0
        profile = store.get(args.project)
        manager = SessionManager(store, NativeKeyring())
        notify = lambda message: print(message, file=sys.stderr)
        if args.command == "login":
            if not 1 <= args.timeout <= 900:
                raise CredentialError("--timeout must be between 1 and 900 seconds.")
            session = manager.login(profile, notify, no_browser=args.no_browser, timeout=args.timeout)
            print(f"Signed in as {session.user_id} for '{profile.name}' (team {session.team_id}).")
        elif args.command == "token":
            print(manager.token(profile))
        elif args.command == "status":
            print(json.dumps(manager.status(profile), indent=2))
        elif args.command == "logout":
            manager.logout(profile)
            print(f"Logged out of '{profile.name}'. Previously issued access tokens expire naturally.")
        elif args.command == "run":
            arguments = args.agent_args
            if arguments and arguments[0] == "--":
                arguments = arguments[1:]
            return launch(args.agent, profile, manager.token(profile), arguments, args.model)
        return 0
    except CredentialError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
