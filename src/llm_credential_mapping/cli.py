import argparse
import json
import os
import sys
from functools import cached_property
from pathlib import Path

from . import __version__
from .adapters import Api, Auth, Credential, Gateway, IntegrationContext
from .customers import (
    Attribution, Rules, RulesLoader, Tag, resolve_customer, unmapped_error, validate_customer, write_customer_file,
)
from .errors import CredentialError
from .profiles import Profile, ProfileStore, default_home
from .integrations.claude import Claude
from .integrations.common import header_variable
from .registry import AUTH, GATEWAYS, INTEGRATIONS, auth_for, gateway_for
from .repositories import Checkout, RepositoryStore, find_checkout
from .shells import SHELLS, assignments, hook, pwsh_loader, without_daemon
from .storage import NativeKeyring


CLAUDE = Claude()


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
        return auth_for(self.profile, self.store, NativeKeyring)  # Only kinds that store credentials open the vault.

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

    @cached_property
    def rules(self) -> Rules | None:
        """The profile's central rules; a credential is only requested for rules on the gateway's own origin."""
        return RulesLoader(self.store, self.profile, self.token)()

    def resolve(self, directory: Path, checkout: Checkout | None, explicit: str | None = None) -> Attribution | None:
        return resolve_customer(
            self.store, self.profile, directory, checkout, explicit=explicit, rules=lambda: self.rules,
        )


def launch(agent: str, context: IntegrationContext, attribution: Attribution, arguments: list[str], model: str | None) -> int:
    return INTEGRATIONS[agent].launch(context, attribution, arguments, model)


def _directory(args: argparse.Namespace) -> Path:
    directory = getattr(args, "directory", None)
    try:
        return (directory.expanduser() if directory is not None else Path.cwd()).resolve()
    except (OSError, RuntimeError):
        raise CredentialError("Cannot resolve the directory.") from None


def _catalogue(context: Context, prefix: str | None) -> list[Tag]:
    """Customers to choose from: the central rules' catalogue when it has one, otherwise the gateway's list."""
    if prefix is not None:
        validate_customer(prefix)
    if context.rules is not None and context.rules.customers:
        customers = list(context.rules.customers)
    else:
        customers = context.gateway.visible_customers(context.credential())
        if customers is None:
            raise CredentialError(
                "Neither central rules nor this gateway offer a customer catalogue; pass --customer with an approved customer."
            )
    return [customer for customer in customers if prefix is None or customer.name.startswith(prefix)]


def _pick_customer(context: Context, prefix: str | None) -> str:
    if not sys.stdin.isatty():
        raise CredentialError("No customer selected. Use --customer NAME, or choose one in an interactive terminal.")
    tags = _catalogue(context, prefix)
    if not tags:
        raise CredentialError(
            "No visible customers matched. A gateway's list is often empty for single sign-on users; "
            "ask an administrator for a customers catalogue in the central rules, or pass --customer with an approved customer."
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
    # Each auth kind validates its own settings and rejects ones it does not use.
    settings = {
        name: value for name, value in (("team_id", args.team_id), ("host_profile", args.host_profile)) if value is not None
    }
    profile = Profile(
        args.name, args.gateway, settings, args.gateway_type, args.auth, args.allow_insecure_localhost, args.rules,
    )
    context.store.add(profile)
    print(f"Added profile '{profile.name}' ({profile.gateway_type} gateway, {profile.auth_type} sign-in).")


def profile_update(context: Context) -> None:
    args = context.args
    profile = context.store.set_rules(args.name, None if args.no_rules else args.rules)
    rules = json.dumps(profile.rules) if profile.rules is not None else "no central rules"
    print(f"Profile '{profile.name}' now uses {rules}. The sign-in is unchanged.")


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
        "rules": profile.rules,
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
    if context.args.require_attribution_env:
        # For tools that send attribution headers from environment variables (Codex): no customer, no credential.
        missing = [
            header_variable(name) for name in context.gateway.attribution_header_names()
            if not os.environ.get(header_variable(name), "").strip()
        ]
        if missing:
            raise CredentialError(
                f"No customer for this session ({', '.join(missing)} not set). Add the hook from "
                "'llm-credentials shell-init SHELL' to your shell profile, or start the tool with 'llm-credentials run'."
            )
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
    context.auth.logout(notify)
    print(f"Logged out of '{context.profile.name}'. Previously issued access tokens expire naturally.")


# Customers, repositories and agents


def tags_list(context: Context) -> None:
    tags = _catalogue(context, context.args.prefix)
    if context.args.json:
        print(json.dumps([{"name": tag.name, "description": tag.description} for tag in tags], indent=2))
        return
    for tag in tags:
        print(f"{json.dumps(tag.name)}\t{json.dumps(tag.description)}")
    notify(f"{len(tags)} visible customer(s). Visibility is not customer authorization.")


def _print_environment(context: Context, shell: str, directory: Path, checkout: Checkout | None) -> None:
    """Statements setting the attribution header variables for this directory, or unsetting them; never fails the shell."""
    values: dict[str, str | None] = {
        header_variable(name): None for name in context.gateway.attribution_header_names()
    }
    try:
        attribution = context.resolve(directory, checkout)
        if attribution is not None:
            for name, value in context.gateway.attribution_headers(attribution).items():
                values[header_variable(name)] = value
    except CredentialError as error:
        notify(f"llm-credentials: {error}")
    print(assignments(shell, values))


def shell_init(context: Context) -> None:
    profile = ["--profile", context.args.profile] if context.args.profile else []
    if context.args.terminal:
        if context.args.shell != "pwsh":
            raise CredentialError("--terminal is for PowerShell; other shells load the hook from their profile.")
        _print_pwsh_terminal([*_integration_context(context).cli_command("shell-init", "pwsh"), *profile])
        return
    argv = [*_integration_context(context).cli_command("repo", "show", "--env", context.args.shell), *profile]
    print(hook(context.args.shell, argv))
    print(without_daemon(context.args.shell))


def _print_pwsh_terminal(argv: list[str]) -> None:
    args = pwsh_loader(argv)
    name = "PowerShell (llm-credentials)"
    vscode = {
        "terminal.integrated.profiles.windows": {name: {"source": "PowerShell", "args": args}},
        "terminal.integrated.defaultProfile.windows": name,
    }
    print("# Windows Terminal: Settings > your PowerShell profile > Command line (PowerShell 7: pwsh.exe instead):")
    print(f'powershell.exe {args[0]} {args[1]} "{args[2]}"')
    print("# VS Code: add to your user settings (JSON):")
    print(json.dumps(vscode, indent=2)[1:-1].strip("\n"))


def _origin(checkout: Checkout | None) -> str:
    if checkout is None or checkout.origin is None:
        raise CredentialError(
            "Personal overrides need a Git checkout with an HTTP/SSH origin remote. "
            "Use 'repo bind --shared' to assign a folder with a .llm-customer file instead."
        )
    return checkout.origin


def repo_show(context: Context) -> None:
    directory = _directory(context.args)
    checkout = find_checkout(directory)
    if context.args.env:
        _print_environment(context, context.args.env, directory, checkout)
        return
    attribution = context.resolve(directory, checkout)
    if attribution is None:
        raise unmapped_error(directory, checkout)
    headers = context.gateway.attribution_headers(attribution)
    if context.args.json:
        print(json.dumps({**attribution.to_dict(), "headers": headers}))
        return
    print(f"Customer: {json.dumps(attribution.customer)}")
    print(f"Source: {attribution.source} ({attribution.detail})")
    print(f"Repository: {attribution.repository or 'none'}")


def repo_list(context: Context) -> None:
    print(json.dumps(RepositoryStore(context.store).list(context.profile.gateway), indent=2))


def repo_bind(context: Context) -> None:
    args = context.args
    directory = _directory(args)
    checkout = find_checkout(directory)
    repository = None if args.shared else _origin(checkout)
    customer = validate_customer(args.customer) if args.customer is not None else _pick_customer(
        context, args.tag_prefix,
    )
    if args.shared:
        path = write_customer_file(checkout.root if checkout is not None else directory, customer)
        print(f"Wrote {path} for {json.dumps(customer)}. Commit it to share it with your team.")
        resolved = context.resolve(directory, checkout)
        if resolved is not None and resolved.source != "file":
            notify(f"Note: your {resolved.source} assignment ({resolved.detail}) still takes precedence here.")
    else:
        RepositoryStore(context.store).bind(context.profile.gateway, repository, customer, replace=True)
        print(f"Bound {json.dumps(repository)} to {json.dumps(customer)} for you only.")
    _refresh_claude(context, directory, checkout)


def repo_unbind(context: Context) -> None:
    directory = _directory(context.args)
    checkout = find_checkout(directory)
    repository = _origin(checkout)
    RepositoryStore(context.store).unbind(context.profile.gateway, repository)
    print(f"Removed your override for {json.dumps(repository)}.")
    _refresh_claude(context, directory, checkout)


def run(context: Context) -> int:
    args = context.args
    profile = context.profile
    directory = _directory(args)
    checkout = find_checkout(directory)
    attribution = context.resolve(directory, checkout, args.customer)
    if attribution is None:
        if checkout is None or checkout.origin is None or not sys.stdin.isatty():
            raise unmapped_error(directory, checkout)
        customer = _pick_customer(context, args.tag_prefix)
        RepositoryStore(context.store).bind(profile.gateway, checkout.origin, customer)
        notify("Saved your personal customer override for this repository.")
        attribution = Attribution(customer, "local", f"repo bind for {checkout.origin}", checkout.origin)
    arguments = args.agent_args
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    notify(
        f"Customer {json.dumps(attribution.customer)} ({attribution.source}: {attribution.detail}) "
        f"is pinned for this process (profile {profile.name})."
    )
    return launch(args.agent, _integration_context(context, context.token()), attribution, arguments, args.model)


# Tool setup and the Claude prompt guard


def _integration_context(context: Context, token: str = "") -> IntegrationContext:
    return IntegrationContext(context.profile, context.gateway, token, context.store.home)


def setup_tool(context: Context) -> None:
    args = context.args
    integration = INTEGRATIONS[args.tool]
    if args.remove:
        notes = integration.remove(_integration_context(context))
    else:
        # Fail now, not later in the tool, when the profile is not signed in; some tools list models with it.
        token = context.token()
        notes = integration.setup(_integration_context(context, token), args.model, args.replace)
    for note in notes:
        print(note)


def claude_guard(context: Context) -> None:
    """UserPromptSubmit hook: print a block decision unless the prompt will carry the right customer.

    Fails closed: any error blocks the prompt with its message instead of letting it through untagged.
    """
    try:
        data = json.loads(sys.stdin.read(1_048_576) or "{}")
    except ValueError:
        data = {}
    # Claude reads local settings for the directory it started in, which the payload's cwd leaves after a cd.
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or (data.get("cwd") if isinstance(data, dict) else None)
    try:
        directory = Path(cwd).resolve() if isinstance(cwd, str) and cwd else Path.cwd().resolve()
        checkout = find_checkout(directory)
        attribution = context.resolve(directory, checkout)
        reason = CLAUDE.guard(
            _integration_context(context), directory, checkout.root if checkout is not None else None,
            attribution, str(unmapped_error(directory, checkout)), os.environ,
        )
    except CredentialError as error:
        reason = f"llm-credentials: {error}"
    except Exception:  # noqa: BLE001 - a crashing guard must not let the prompt through.
        reason = "llm-credentials could not check the customer for this prompt; run 'llm-credentials repo show'."
    if reason is not None:
        print(json.dumps({"decision": "block", "reason": reason}))


def _refresh_claude(context: Context, directory: Path, checkout: Checkout | None) -> None:
    """Rewrite Claude local settings this tool wrote, so running sessions follow a changed assignment."""
    root = checkout.root if checkout is not None else None
    attribution = context.resolve(directory, checkout)
    integration = _integration_context(context)
    for folder in dict.fromkeys(folder for folder in (root, directory) if folder is not None):
        path = folder / ".claude" / "settings.local.json"
        if CLAUDE.refresh_local(integration, path, attribution, root):
            notify(f"Updated {path}; running Claude sessions use the new customer from their next prompt.")


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
    add.add_argument(
        "--host-profile",
        help="forwarded: the profile on your local machine whose sign-in VS Code relays (default: its selected profile).",
    )
    add.add_argument("--allow-insecure-localhost", action="store_true", help="Development only.")
    add.add_argument("--rules", help="Central repository-to-customer rules: an HTTPS URL or an absolute file path.")
    add.set_defaults(handler=profile_add)
    update = profile_actions.add_parser("update", help="Change a profile's central customer rules.")
    update.add_argument("name")
    rules = update.add_mutually_exclusive_group(required=True)
    rules.add_argument("--rules", help="An HTTPS URL or an absolute file path.")
    rules.add_argument("--no-rules", action="store_true", help="Stop using central rules.")
    update.set_defaults(handler=profile_update)
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
    command.add_argument(
        "--require-attribution-env", action="store_true",
        help="Refuse unless the attribution header variables are set (for tools reading them from the environment).",
    )
    command.set_defaults(handler=token)
    command = commands.add_parser("shell-init", help="Print a shell profile hook that sets the customer per directory.")
    command.add_argument("shell", choices=SHELLS)
    command.add_argument(
        "--terminal", action="store_true",
        help="PowerShell only: print a terminal command line that loads the hook without a script file "
        "(for machines where the execution policy blocks profiles).",
    )
    _profile_option(command)
    command.set_defaults(handler=shell_init)
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

    repo = commands.add_parser("repo", help="Show and assign the customer of a repository or folder.")
    repo_actions = repo.add_subparsers(dest="action", required=True)
    for action, handler, text in (
        ("show", repo_show, "Show the customer for a directory and why; exit 1 when none is assigned."),
        ("bind", repo_bind, "Assign a customer: a personal override, or with --shared a committed .llm-customer file."),
        ("list", repo_list, "List your personal overrides."),
        ("unbind", repo_unbind, "Remove your personal override for a repository."),
    ):
        command = repo_actions.add_parser(action, help=text)
        _profile_option(command)
        if action != "list":
            command.add_argument("--directory", type=Path, help="Repository or folder; otherwise the current directory.")
        if action == "show":
            output = command.add_mutually_exclusive_group()
            output.add_argument("--json", action="store_true", help="Print one line of JSON including the request headers.")
            output.add_argument(
                "--env", choices=SHELLS,
                help="Print statements setting the attribution header variables (unsetting them when unassigned).",
            )
        if action == "bind":
            command.add_argument("--customer", help="Exact approved customer; otherwise choose from the catalogue.")
            command.add_argument("--tag-prefix", help="Filter the customer picker by this exact prefix.")
            command.add_argument(
                "--shared", action="store_true",
                help="Write .llm-customer at the checkout root (or the directory outside Git) to commit for your team.",
            )
        command.set_defaults(handler=handler)

    command = commands.add_parser("run", help="Launch an agent with the repository's customer attribution.")
    _profile_option(command)
    command.add_argument("--customer", help="Exact customer for this process only; does not change the repo default.")
    command.add_argument("--tag-prefix", help="Filter the customer picker for an unbound repository.")
    command.add_argument("--model", help="Gateway model alias; required for OpenCode.")
    command.add_argument("agent", choices=sorted(INTEGRATIONS))
    command.add_argument("agent_args", nargs=argparse.REMAINDER)
    command.set_defaults(handler=run)

    command = commands.add_parser("setup", help="Configure a tool once to use this CLI for credentials and customers.")
    command.add_argument("tool", choices=sorted(INTEGRATIONS))
    _profile_option(command)
    command.add_argument("--model", help="Default model alias for the tool.")
    command.add_argument("--replace", action="store_true", help="Replace another tool's credential configuration.")
    command.add_argument("--remove", action="store_true", help="Remove what setup configured; keep everything else.")
    command.set_defaults(handler=setup_tool)

    claude = commands.add_parser("claude", help="Claude Code hooks installed by 'setup claude'.")
    guard = claude.add_subparsers(dest="action", required=True).add_parser(
        "guard", help="UserPromptSubmit hook: assign the customer and block prompts that would be sent without one.",
    )
    _profile_option(guard)
    guard.set_defaults(handler=claude_guard)

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
