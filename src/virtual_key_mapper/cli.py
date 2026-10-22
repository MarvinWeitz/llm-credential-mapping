import argparse
import dataclasses
import json
import os
import re
import sys
from functools import cached_property
from pathlib import Path
from typing import Callable, Sequence

from . import __version__
from .adapters import Api, Auth, Credential, Gateway, IntegrationContext
from .errors import CredentialError
from .labels import (
    MAX_VALUE, NO_LABELS_NOTICE, SOURCES, Attribution, Choice, Label, LabelConfig, LabelDefinition, MissingLabelsError,
    OutputOverride, definitions_from_dict, env_name, validate_key, validate_value,
)
from .profiles import Profile, ProfileStore, default_home, with_label, with_output, without_label
from .integrations.claude import Claude
from .integrations.common import (
    ATTRIBUTION_VARIABLE, HEADER_PREFIX, header_environment, header_variable, launch_labels,
    require_attribution_environment,
)
from .registry import AUTH, GATEWAYS, INTEGRATIONS, auth_for, gateway_for, gateway_outputs
from .repositories import Checkout, RepositoryStore, find_checkout
from .resolve import (
    failed_document, label_config, load_config, missing_error, resolve_labels, show_document, write_label_file,
)
from .rules import RulesLoader
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
    def labels(self) -> LabelConfig:
        """The labels and outputs in effect; a credential is only requested for rules on the gateway's own origin."""
        return load_config(self.store, self.profile, self.gateway.outputs(), self.token)

    def resolve(
        self, directory: Path, checkout: Checkout | None, explicit: dict[str, str | None] | None = None,
        unknown_explicit: str = "error",
    ) -> Attribution:
        return resolve_labels(
            self.store, self.profile, self.labels, directory, checkout, explicit=explicit, unknown_explicit=unknown_explicit,
        )

    def notices(self) -> None:
        """For text commands only: dropped profile pieces, document remarks, and when no label is defined."""
        for notice in self.labels.notices:
            notify(notice)
        if not self.labels.definitions:
            notify(NO_LABELS_NOTICE)


def launch(agent: str, context: IntegrationContext, attribution: Attribution, arguments: list[str], model: str | None) -> int:
    return INTEGRATIONS[agent].launch(context, attribution, arguments, model)


def _directory(args: argparse.Namespace) -> Path:
    directory = getattr(args, "directory", None)
    try:
        return (directory.expanduser() if directory is not None else Path.cwd()).resolve()
    except (OSError, RuntimeError):
        raise CredentialError("Cannot resolve the directory.") from None


# Label values from the command line and the terminal picker


_SOURCE_OPTIONS = {"explicit": "--label", "local": "a personal binding", "file": "a .vkm file"}


def _undefined(config: LabelConfig, key: str) -> CredentialError:
    return CredentialError(f"No label '{key}' is defined; defined labels: {', '.join(config.keys) or 'none'}.")


def _label_values(config: LabelConfig, pairs: Sequence[str] | None, source: str) -> dict[str, str]:
    """``--label KEY=VALUE`` arguments checked against the definitions; ``KEY=`` gives "" (no value)."""
    values: dict[str, str] = {}
    for pair in pairs or ():
        key, separator, value = pair.partition("=")
        if not separator:
            raise CredentialError(f"Use --label KEY=VALUE, not {json.dumps(pair[:80])}.")
        validate_key(key)
        if key in values:
            raise CredentialError(f"--label {key} is given more than once.")
        definition = config.get(key)
        if definition is None:
            raise _undefined(config, key)
        if source not in definition.sources:
            raise CredentialError(
                f"Label '{key}' cannot be set with {_SOURCE_OPTIONS[source]}; its sources are: {', '.join(definition.sources)}."
            )
        if value:
            validate_value(value, key)
            if not definition.accepts(value):
                raise CredentialError(
                    f"{json.dumps(value)} is not one of the values of '{key}' (see 'vkm labels values {key}')."
                )
        values[key] = value
    return values


def _offered(context: Context, definition: LabelDefinition) -> tuple[list[Choice] | None, str | None]:
    """Values to choose from and where they come from: the definition's, the gateway's catalogue, or none."""
    if definition.values is not None:
        return list(definition.values), "values"
    catalogue = context.gateway.catalogue(definition.key, context.credential, context.labels)
    return (catalogue, "catalogue") if catalogue is not None else (None, None)


def _pick(context: Context, definition: LabelDefinition) -> str:
    """Ask for one value: a listed one when values are offered, otherwise any valid typed value."""
    key = definition.key
    choices, origin = _offered(context, definition)
    description = f" - {json.dumps(definition.description)}" if definition.description else ""
    notify(f"Label {key}{description}")
    if choices:
        if origin == "catalogue":
            notify("The gateway's list is not an authorization list. Choose the approved value:")
        for index, choice in enumerate(choices, 1):
            text = choice.name or choice.description
            notify(f"{index}. {json.dumps(choice.id)}{f' - {json.dumps(text)}' if text else ''}")
        print(f"{key} (number or exact value): ", end="", file=sys.stderr, flush=True)
    else:
        if origin == "catalogue":
            notify(f"The gateway lists no values for '{key}'.")
        print(f"{key} (value): ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip()
    if not answer or len(answer) > MAX_VALUE:
        raise CredentialError(f"No value chosen for '{key}'; nothing was saved. Pass --label {key}=VALUE instead.")
    if choices:
        if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1].id
        if any(choice.id == answer for choice in choices):
            return answer
        raise CredentialError(f"Invalid choice for '{key}'; choose a displayed value or number, or pass --label {key}=VALUE.")
    return validate_value(answer, key)


def _ask(context: Context, keys: Sequence[str]) -> dict[str, str]:
    """Ask in the terminal for each key; nothing is returned (or saved) unless every answer is valid."""
    return {key: _pick(context, context.labels.get(key)) for key in keys}


def _describe_label(label: Label, definition: LabelDefinition) -> str:
    required = " (required)" if definition.required else ""
    if label.value is None:
        text = f"{label.key}{required}: no value"
    else:
        choice = definition.choice(label.value)
        name = f" ({json.dumps(choice.name)})" if choice is not None and choice.name else ""
        text = f"{label.key}{required} = {json.dumps(label.value)}{name}"
    return text + (f"  [{label.source}: {label.detail}]" if label.source is not None else "")


def _describe_labels(config: LabelConfig, attribution: Attribution) -> list[str]:
    """Each label with its value, source and detail, then the ignored values; for people, not tools."""
    lines = [
        _describe_label(attribution.get(definition.key) or Label(definition.key, None), definition)
        for definition in config.definitions
    ]
    for item in attribution.ignored:
        value = f" = {json.dumps(item.value)}" if item.value is not None else ""
        lines.append(f"Ignored: {item.key}{value}  [{item.source}: {item.detail}]: {item.reason}")
    return lines


def _assigned(labels: dict[str, str]) -> str:
    return ", ".join(f"{key}={json.dumps(value) if value else 'no value'}" for key, value in labels.items())


# Profiles and sessions


def _definitions(args: argparse.Namespace, profile: Profile) -> Profile:
    """Apply the label options of 'profile add/update' in order: --definitions, --no-label, --label, outputs."""
    if args.label is None and (args.required or args.description is not None or args.value or args.strict or args.source):
        raise CredentialError("--required, --description, --value, --strict and --source describe the label of --label KEY.")
    if args.definitions is not None:
        try:
            data = json.loads(args.definitions)
        except (ValueError, RecursionError):
            raise CredentialError('--definitions must be JSON such as {"labels": [...], "outputs": {...}}.') from None
        labels, outputs = definitions_from_dict(data, "--definitions")
        profile = dataclasses.replace(profile, labels=labels, outputs=outputs)
    for key in args.no_label or ():
        profile = without_label(profile, validate_key(key))
    if args.label is not None:
        values = tuple(Choice(value, name or None) for value, _, name in (item.partition("=") for item in args.value or ()))
        profile = with_label(profile, LabelDefinition(
            args.label, args.required, args.description, values or None, args.strict,
            tuple(args.source) if args.source else SOURCES,
        ))
    formats: dict[str, list[str]] = {}
    for item in args.output or ():
        name, separator, text = item.partition("=")
        if not separator:
            raise CredentialError(f"Use --output NAME=FORMAT, not {json.dumps(item[:80])}.")
        formats.setdefault(name, []).append(text)
    changes = [
        *((name, OutputOverride(tuple(texts))) for name, texts in formats.items()),
        *((name, OutputOverride(None, False)) for name in args.output_off or ()),
        *((name, None) for name in args.output_default or ()),
    ]
    names = [output.name for output in gateway_outputs(profile.gateway_type)]
    changed = set()
    for name, override in changes:
        if name not in names:
            raise CredentialError(f"The {profile.gateway_type} gateway has no output '{name}'; it has: {', '.join(names)}.")
        if name in changed:
            raise CredentialError(f"Output '{name}' is changed by more than one option.")
        changed.add(name)
        profile = with_output(profile, name, override)
    return profile


def _check_profile(store: ProfileStore, profile: Profile, token: Callable[[], str] | None) -> None:
    """Refuse what can be checked before use: the profile with its adapter, and with the rules when they can be read."""
    outputs = gateway_outputs(profile.gateway_type)
    if profile.rules is None:
        label_config(profile, outputs, None)
        return
    try:
        rules = RulesLoader(store, profile, token)()
    except CredentialError as error:
        notify(f"The label rules could not be read to check the profile against them: {error}")
        # Still refuse what is wrong whatever the rules define, e.g. a required profile label that reaches no output.
        LabelConfig.build(outputs, profile_labels=profile.labels, profile_outputs=profile.outputs, unread_central=True)
        return
    if rules is not None:
        central = {env_name(definition.key): definition.key for definition in rules.labels}
        for definition in profile.labels:
            if env_name(definition.key) in central:
                raise CredentialError(
                    f"The label rules define '{central[env_name(definition.key)]}', so the profile cannot define "
                    f"'{definition.key}'; remove it with --no-label {definition.key}."
                )
    for notice in label_config(profile, outputs, rules).notices:
        notify(notice)


def profile_add(context: Context) -> None:
    args = context.args
    # Each auth kind validates its own settings and rejects ones it does not use.
    settings = {
        name: value for name, value in (("team_id", args.team_id), ("host_profile", args.host_profile)) if value is not None
    }
    profile = _definitions(args, Profile(
        args.name, args.gateway, settings, args.gateway_type, args.auth, args.allow_insecure_localhost, args.rules,
    ))
    _check_profile(context.store, profile, None)  # A new profile has no sign-in yet.
    context.store.add(profile)
    print(f"Added profile '{profile.name}' ({profile.gateway_type} gateway, {profile.auth_type} sign-in).")


def profile_update(context: Context) -> None:
    args = context.args

    def change(profile: Profile) -> Profile:
        if args.rules is not None or args.no_rules:
            profile = dataclasses.replace(profile, rules=args.rules)
        return _definitions(args, profile)

    changed = change(context.store.get(args.name))
    if args.rules is None and not args.no_rules and not (
        args.label or args.no_label or args.output or args.output_off or args.output_default or args.definitions is not None
    ):
        raise CredentialError(
            "Nothing to change: pass --rules, --no-rules, --label, --no-label, --output, --output-off, --output-default "
            "or --definitions."
        )
    _check_profile(context.store, changed, lambda: auth_for(changed, context.store, NativeKeyring).credential().secret)
    profile = context.store.update(args.name, change)
    rules = json.dumps(profile.rules) if profile.rules is not None else "no central rules"
    labels = ", ".join(definition.key for definition in profile.labels) or "none"
    print(f"Profile '{profile.name}' now uses {rules}; its own labels: {labels}. The sign-in is unchanged.")


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
        # The profile's own definitions, not merged with the central rules (the dev container relay copies them).
        "labels": [],
        "outputs": {},
        **profile.definitions(),
    }
    if context.args.json:
        print(json.dumps(value))
        return
    print(json.dumps(value, indent=2))
    try:
        notices = load_config(context.store, profile, gateway.outputs()).notices  # Without a credential.
    except CredentialError as error:
        notices = (f"The label configuration cannot be used: {error}",)
    for notice in notices:
        notify(notice)


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
        # For tools that send the label headers from environment variables (Codex): no labels, no credential.
        require_attribution_environment(os.environ)
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


# Labels, repositories and agents


def labels_list(context: Context) -> None:
    config = context.labels
    if context.args.json:
        print(json.dumps([{**definition.to_dict(), "origin": definition.origin} for definition in config.definitions], indent=2))
        return
    for definition in config.definitions:
        kind = "required" if definition.required else "optional"
        origin = "central rules" if definition.origin == "rules" else "profile"
        description = f": {json.dumps(definition.description)}" if definition.description else ""
        print(f"{definition.key} ({kind}, {origin}){description}")
        if definition.values is not None:
            values = ", ".join(json.dumps(choice.id) for choice in definition.values)
            print(f"  values{' (strict)' if definition.strict else ''}: {values}")
        if definition.sources != SOURCES:
            print(f"  sources: {', '.join(definition.sources)}")
    context.notices()


def labels_values(context: Context) -> None:
    key = validate_key(context.args.key)
    definition = context.labels.get(key)
    if definition is None:
        raise _undefined(context.labels, key)
    choices, origin = _offered(context, definition)
    if context.args.json:
        print(json.dumps({
            "key": key, "source": origin,
            "values": None if choices is None else [dataclasses.asdict(choice) for choice in choices],
        }, indent=2))
        return
    if choices is None:
        notify(f"'{key}' lists no values and the gateway has no catalogue for it; any valid value is accepted.")
        return
    for choice in choices:
        text = choice.name or choice.description
        print(json.dumps(choice.id) + (f"\t{json.dumps(text)}" if text else ""))
    if origin == "values":
        notify(f"{len(choices)} value(s) from the definition of '{key}'.")
    else:
        notify(f"{len(choices)} value(s) from the gateway's catalogue. Visibility is not authorization.")


def _print_environment(context: Context, shell: str) -> None:
    """Statements setting the header variables for this directory, or unsetting them; never fails the shell."""
    try:
        directory = _directory(context.args)
        attribution = context.resolve(directory, find_checkout(directory), _show_explicit(context), "ignore")
        values = header_environment(_integration_context(context), attribution)
    except Exception as error:  # noqa: BLE001 - whatever failed, no other folder's labels may stay set.
        # Missing required labels unset everything quietly, like a folder without labels on every cd.
        if not isinstance(error, CredentialError):
            notify("vkm: the labels for this directory could not be resolved; run 'vkm repo show'.")
        elif not isinstance(error, MissingLabelsError):
            notify(f"vkm: {error}")
        values = {}
        try:
            values.update({header_variable(output.header): None for output in context.gateway.outputs()})
        except Exception:  # noqa: BLE001 - the profile may be what failed; the environment still names the variables.
            pass
        values.update({name: None for name in os.environ if re.fullmatch(HEADER_PREFIX + "[A-Z0-9_]+", name)})
        values[ATTRIBUTION_VARIABLE] = None
    print(assignments(shell, values))


def shell_init(context: Context) -> None:
    profile = ["--profile", context.args.profile] if context.args.profile else []
    integration = _integration_context(context, labels=False)
    if context.args.terminal:
        if context.args.shell != "pwsh":
            raise CredentialError("--terminal is for PowerShell; other shells load the hook from their profile.")
        _print_pwsh_terminal([*integration.cli_command("shell-init", "pwsh"), *profile])
        return
    argv = [*integration.cli_command("repo", "show", "--env", context.args.shell), *profile]
    print(hook(context.args.shell, argv, (ATTRIBUTION_VARIABLE,)))
    print(without_daemon(context.args.shell))


def _print_pwsh_terminal(argv: list[str]) -> None:
    args = pwsh_loader(argv)
    name = "PowerShell (vkm)"
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
            "Personal bindings need a Git checkout with an HTTP/SSH origin remote. "
            "Use 'repo bind --shared' to label a folder with a .vkm file instead."
        )
    return checkout.origin


def _show_explicit(context: Context) -> dict[str, str | None]:
    """'repo show' explicit labels: the launch marker's with --launch, then --label, which wins per key."""
    explicit: dict[str, str | None] = launch_labels(os.environ) if context.args.launch else {}
    explicit.update(_label_values(context.labels, context.args.label, "explicit"))
    return explicit


def repo_show(context: Context) -> None:
    if context.args.env:
        _print_environment(context, context.args.env)
        return
    directory = _directory(context.args)
    checkout = find_checkout(directory)
    config = context.labels
    if context.args.json:
        # Tools read this document: once the configuration loads, a resolution error is reported in it, not by exit 1.
        explicit = _show_explicit(context)
        try:
            attribution = context.resolve(directory, checkout, explicit, "ignore")
        except CredentialError as error:
            print(json.dumps(failed_document(config, error, checkout)))
        else:
            print(json.dumps(show_document(config, attribution, directory, checkout)))
        return
    # --label keys are checked here; marker keys this profile does not define are only listed as ignored.
    attribution = context.resolve(directory, checkout, _show_explicit(context), "ignore")
    print(f"Repository: {attribution.repository or 'none'}")
    for line in _describe_labels(config, attribution):
        print(line)
    context.notices()
    if not attribution.complete:
        raise missing_error(attribution, directory, checkout)
    config.headers(attribution)  # Exit 1 as well when the headers cannot be built.


def repo_list(context: Context) -> None:
    print(json.dumps(RepositoryStore(context.store).list(context.profile.gateway), indent=2))


def repo_bind(context: Context) -> None:
    args = context.args
    directory = _directory(args)
    checkout = find_checkout(directory)
    repository = None if args.shared else _origin(checkout)
    source = "file" if args.shared else "local"
    labels = _label_values(context.labels, args.label, source)
    if not labels:
        missing = context.resolve(directory, checkout).missing
        if not missing:
            print("Every required label has a value here; pass --label KEY=VALUE to set others.")
            return
        # Only ask for what this kind of binding may set; a label's sources may exclude it.
        settable = [key for key in missing if source in context.labels.get(key).sources]
        for key in missing:
            if key not in settable:
                notify(
                    f"'{key}' cannot be set with {_SOURCE_OPTIONS[source]}; "
                    f"its sources are: {', '.join(context.labels.get(key).sources)}."
                )
        if not settable:
            raise CredentialError(f"Nothing to ask: no missing required label can be set with {_SOURCE_OPTIONS[source]}.")
        if not sys.stdin.isatty():
            raise CredentialError(
                f"No --label given; pass --label KEY=VALUE for {', '.join(settable)}, or run this in a terminal to choose."
            )
        labels = _ask(context, settable)
    if args.shared:
        path = write_label_file(checkout.root if checkout is not None else directory, labels)
        print(f"Wrote {path} with {_assigned(labels)}. Commit it to share it with your team.")
        resolved = context.resolve(directory, checkout)
        for key in labels:
            label = resolved.get(key)
            if label is not None and label.source not in (None, "file"):
                notify(f"Note: '{key}' still comes from {label.source} ({label.detail}) here.")
    else:
        if not all(labels.values()):
            raise CredentialError("A personal binding needs values; remove one with 'repo unbind --label KEY'.")
        merged = RepositoryStore(context.store).bind(context.profile.gateway, repository, labels, replace=args.replace)
        print(f"Bound {json.dumps(repository)} to {_assigned(merged)} for you only.")
    _refresh_claude(context, directory, checkout)


def repo_unbind(context: Context) -> None:
    directory = _directory(context.args)
    checkout = find_checkout(directory)
    repository = _origin(checkout)
    keys = [validate_key(key) for key in context.args.label] if context.args.label else None
    RepositoryStore(context.store).unbind(context.profile.gateway, repository, keys)
    removed = f"{', '.join(keys)} from your binding" if keys else "your binding"
    print(f"Removed {removed} for {json.dumps(repository)}.")
    _refresh_claude(context, directory, checkout)


def run(context: Context) -> int:
    args = context.args
    profile = context.profile
    directory = _directory(args)
    checkout = find_checkout(directory)
    config = context.labels
    explicit = _label_values(config, args.label, "explicit")
    attribution = context.resolve(directory, checkout, explicit)
    context.notices()
    if attribution.missing:
        # A personal binding can only answer below an explicit "no value" and for labels that accept bindings.
        bindable = all(
            attribution.get(key).source != "explicit" and "local" in config.get(key).sources for key in attribution.missing
        )
        if not bindable or checkout is None or checkout.origin is None or not sys.stdin.isatty():
            raise missing_error(attribution, directory, checkout)
        answers = _ask(context, attribution.missing)
        RepositoryStore(context.store).bind(profile.gateway, checkout.origin, answers)
        notify(f"Saved {_assigned(answers)} as your personal binding for {json.dumps(checkout.origin)}.")
        attribution = context.resolve(directory, checkout, explicit)
        if attribution.missing:
            raise missing_error(attribution, directory, checkout)
    arguments = args.agent_args
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    notify(f"Labels pinned for this process (profile {profile.name}):")
    for line in _describe_labels(config, attribution):
        notify(f"  {line}")
    return launch(args.agent, _integration_context(context, context.token()), attribution, arguments, args.model)


# Tool setup and the Claude prompt guard


def _integration_context(context: Context, token: str = "", *, labels: bool = True) -> IntegrationContext:
    """``labels=False`` skips loading the rules where only the CLI command or the stored setup is needed."""
    return IntegrationContext(
        context.profile, context.gateway, token, context.store.home, labels=context.labels if labels else None,
    )


def setup_tool(context: Context) -> None:
    args = context.args
    integration = INTEGRATIONS[args.tool]
    if args.remove:
        notes = integration.remove(_integration_context(context, labels=False))
    else:
        # Fail now, not later in the tool, when the profile is not signed in; some tools list models with it.
        token = context.token()
        notes = integration.setup(_integration_context(context, token), args.model, args.replace)
        context.notices()
    for note in notes:
        print(note)


def claude_guard(context: Context) -> None:
    """UserPromptSubmit hook: print a block decision unless the prompt will carry the right labels.

    Fails closed: any error blocks the prompt with its message instead of letting it through without labels.
    """
    try:
        data = json.loads(sys.stdin.read(1_048_576) or "{}")
    except (ValueError, RecursionError):
        data = {}
    # Claude reads local settings for the directory it started in, which the payload's cwd leaves after a cd.
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or (data.get("cwd") if isinstance(data, dict) else None)
    try:
        directory = Path(cwd).resolve() if isinstance(cwd, str) and cwd else Path.cwd().resolve()
        checkout = find_checkout(directory)
        # A session that 'run claude --label' started passes with its own labels: the launch marker is explicit.
        attribution = context.resolve(directory, checkout, launch_labels(os.environ), "ignore")
        reason = CLAUDE.guard(_integration_context(context), directory, checkout, attribution, os.environ)
    except CredentialError as error:
        reason = f"vkm: {error}"
    except Exception:  # noqa: BLE001 - a crashing guard must not let the prompt through.
        reason = "vkm could not check the labels for this prompt; run 'vkm repo show'."
    if reason is not None:
        print(json.dumps({"decision": "block", "reason": reason}))


def _refresh_claude(context: Context, directory: Path, checkout: Checkout | None) -> None:
    """Rewrite Claude local settings this tool wrote, so running sessions follow changed labels.

    Best effort after a change that is already saved: a failure is a warning (the guard still checks each prompt).
    """
    root = checkout.root if checkout is not None else None
    try:
        attribution = context.resolve(directory, checkout)
        integration = _integration_context(context)
        for folder in dict.fromkeys(folder for folder in (root, directory) if folder is not None):
            path = folder / ".claude" / "settings.local.json"
            if CLAUDE.refresh_local(integration, path, attribution, root):
                notify(f"Updated {path}; running Claude sessions use the new labels from their next prompt.")
    except CredentialError as error:
        notify(f"Warning: Claude local settings were not updated: {error}")


def _profile_option(command: argparse.ArgumentParser) -> None:
    command.add_argument("--profile", help="Authentication profile; otherwise the selected default.")


def _label_option(command: argparse.ArgumentParser, text: str) -> None:
    command.add_argument("--label", action="append", metavar="KEY=VALUE", help=text)


def _definition_options(command: argparse.ArgumentParser) -> None:
    """'profile add/update': the profile's own label definitions and output overrides (docs/labels.md)."""
    command.add_argument("--label", metavar="KEY", help="Define or replace one profile label, described by the next options.")
    command.add_argument("--required", action="store_true", help="Nothing is sent while the label has no value.")
    command.add_argument("--description", help="Shown by the picker and 'labels list'.")
    command.add_argument("--value", action="append", metavar="ID[=NAME]", help="A value the picker offers; repeat for more.")
    command.add_argument("--strict", action="store_true", help="Accept only the --value ids, from any source.")
    command.add_argument(
        "--source", action="append", choices=SOURCES, help="A source that may set the label; repeat (default: all).",
    )
    command.add_argument("--no-label", action="append", metavar="KEY", help="Remove a profile label.")
    command.add_argument(
        "--output", action="append", metavar="NAME=FORMAT",
        help="Override an output, e.g. 'tags=${customer}'; repeat for one NAME to give its whole list.",
    )
    command.add_argument("--output-off", action="append", metavar="NAME", help="Switch an output off.")
    command.add_argument("--output-default", action="append", metavar="NAME", help="Return an output to its default.")
    command.add_argument(
        "--definitions", metavar="JSON", help='Replace the profile\'s labels and outputs: {"labels": [...], "outputs": {...}}.',
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="vkm",
        description="Sign in to an LLM gateway and launch agents with session-local labels for attribution.",
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", type=Path, help="Nonsecret configuration directory.")
    commands = root.add_subparsers(dest="command", required=True)

    profile = commands.add_parser("profile", help="Manage gateway and sign-in profiles.")
    profile_actions = profile.add_subparsers(dest="action", required=True)
    add = profile_actions.add_parser("add", help="Add a profile: the gateway, its sign-in kind, label rules and labels.")
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
    add.add_argument("--rules", help="Central label rules: an HTTPS URL or an absolute file path.")
    _definition_options(add)
    add.set_defaults(handler=profile_add)
    update = profile_actions.add_parser("update", help="Change a profile's label rules, labels or outputs.")
    update.add_argument("name")
    rules = update.add_mutually_exclusive_group()
    rules.add_argument("--rules", help="Central label rules: an HTTPS URL or an absolute file path.")
    rules.add_argument("--no-rules", action="store_true", help="Stop using central label rules.")
    _definition_options(update)
    update.set_defaults(handler=profile_update)
    profile_actions.add_parser("list", help="List the profiles; * marks the selected default.").set_defaults(
        handler=profile_list,
    )
    show = profile_actions.add_parser(
        "show", help="Show a profile, its own labels and its gateway endpoints per API, without credentials.",
    )
    show.add_argument("name", nargs="?", help="Profile name; otherwise the selected default.")
    show.add_argument("--json", action="store_true", help="Print one line of JSON for other tools.")
    show.set_defaults(handler=profile_show)
    use = profile_actions.add_parser("use", help="Select the default profile for later commands.")
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
        help=f"Refuse unless {ATTRIBUTION_VARIABLE} is set, i.e. the session's labels were resolved "
        "(for tools reading the label headers from the environment).",
    )
    command.set_defaults(handler=token)
    command = commands.add_parser("shell-init", help="Print a shell profile hook that sets the label headers per directory.")
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

    labels = commands.add_parser("labels", help="Show the labels in effect and the values they offer.")
    label_actions = labels.add_subparsers(dest="action", required=True)
    command = label_actions.add_parser("list", help="The labels in effect (central and the profile's) and their origin.")
    _profile_option(command)
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=labels_list)
    command = label_actions.add_parser(
        "values", help="A label's values: its definition's, else the gateway's catalogue (not an authorization list).",
    )
    command.add_argument("key")
    _profile_option(command)
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=labels_values)

    repo = commands.add_parser("repo", help="Show and assign the labels of a repository or folder.")
    repo_actions = repo.add_subparsers(dest="action", required=True)
    for action, handler, text in (
        ("show", repo_show, "Show the labels for a directory and their sources; exit 1 while required ones are missing."),
        ("bind", repo_bind, "Assign labels: a personal binding, or with --shared a committed .vkm file."),
        ("list", repo_list, "List your personal bindings."),
        ("unbind", repo_unbind, "Remove your personal binding for a repository, or some of its labels."),
    ):
        command = repo_actions.add_parser(action, help=text)
        _profile_option(command)
        if action != "list":
            command.add_argument("--directory", type=Path, help="Repository or folder; otherwise the current directory.")
        if action == "show":
            _label_option(command, "A label value for this check only (KEY= for no value); repeat for more.")
            command.add_argument(
                "--launch", action="store_true",
                help="Also use the --label values of the 'vkm run' launch this process belongs to (for tool plugins).",
            )
            output = command.add_mutually_exclusive_group()
            output.add_argument("--json", action="store_true", help="Print one line of JSON including the request headers.")
            output.add_argument(
                "--env", choices=SHELLS,
                help="Print statements setting the label header variables (unsetting them while labels are missing).",
            )
        if action == "bind":
            _label_option(command, "A label value; repeat for more. Without it, choose the missing required labels.")
            command.add_argument(
                "--shared", action="store_true",
                help="Write .vkm at the checkout root (or the directory outside Git) to commit for your team.",
            )
            command.add_argument("--replace", action="store_true", help="Change values your binding already has.")
        if action == "unbind":
            command.add_argument("--label", action="append", metavar="KEY", help="Remove only this label; repeat for more.")
        command.set_defaults(handler=handler)

    command = commands.add_parser("run", help="Launch an agent with the repository's labels.")
    _profile_option(command)
    _label_option(command, "A label value for this process only (KEY= for no value); the repository's stay unchanged.")
    command.add_argument("--model", help="Gateway model alias; required for OpenCode.")
    command.add_argument("agent", choices=sorted(INTEGRATIONS))
    command.add_argument("agent_args", nargs=argparse.REMAINDER)
    command.set_defaults(handler=run)

    command = commands.add_parser("setup", help="Configure a tool once to use this CLI for credentials and labels.")
    command.add_argument("tool", choices=sorted(INTEGRATIONS))
    _profile_option(command)
    command.add_argument("--model", help="Default model alias for the tool.")
    command.add_argument("--replace", action="store_true", help="Replace another tool's credential configuration.")
    command.add_argument("--remove", action="store_true", help="Remove what setup configured; keep everything else.")
    command.set_defaults(handler=setup_tool)

    claude = commands.add_parser("claude", help="Claude Code hooks installed by 'setup claude'.")
    guard = claude.add_subparsers(dest="action", required=True).add_parser(
        "guard", help="UserPromptSubmit hook: assign the labels and block prompts that would be sent without them.",
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
