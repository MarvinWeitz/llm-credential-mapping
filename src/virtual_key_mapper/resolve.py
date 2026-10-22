"""Label values for a directory: each key from the first allowed source with an entry (docs/labels.md, section 2)."""

import json
import os
import time
import tomllib
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .errors import CredentialError
from .labels import (
    ENV_PREFIX, NO_LABELS_NOTICE, Attribution, Ignored, Label, LabelConfig, MissingLabelsError, Output, env_name,
    validate_key, validate_value,
)
from .profiles import Profile, ProfileStore, atomic_write_text
from .repositories import RepositoryStore
from .rules import Rules, RulesLoader

LABEL_FILE = ".vkm"
_MAX_FILE_BYTES = 4096
_FILE_HEADER = "# Labels for AI tool requests in this folder and below (vkm).\n"


# Committed per-repository or per-folder file


def find_label_file(directory: Path, home: Path | None = None) -> Path | None:
    """Return the nearest .vkm in ``directory`` or a parent.

    Inside the home directory the search stops below it: the home directory itself and folders
    above it (often shared, like C:\\Users or /home) never assign labels.
    """
    home = (home or Path.home()).resolve()
    for folder in (directory, *directory.parents):
        if folder == home:
            return None
        candidate = folder / LABEL_FILE
        if candidate.is_file():
            return candidate
    return None


def read_label_file(path: Path) -> dict[str, str]:
    """The file's [labels] table in written order; "" is an explicit "no value here"."""
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):  # RecursionError: nesting too deep.
        raise CredentialError(
            f"Cannot read {path}; it must be a small TOML file with a [labels] table such as: customer = \"acme\"."
        ) from None
    if set(data) != {"labels"} or not isinstance(data["labels"], dict):
        raise CredentialError(f"{path} must contain only a [labels] table.")
    try:
        for key, value in data["labels"].items():
            validate_key(key)
            if not isinstance(value, str):
                raise CredentialError(f"The value of '{key}' must be a string.")
            if value:
                validate_value(value, key)
    except CredentialError as error:
        raise CredentialError(f"{path}: {error}") from None
    return dict(data["labels"])


def write_label_file(directory: Path, labels: Mapping[str, str], home: Path | None = None) -> Path:
    """Merge ``labels`` into ``directory``/.vkm and rewrite it; "" writes an explicit "no value here"."""
    path = directory / LABEL_FILE
    home = (home or Path.home()).resolve()
    if directory.resolve() in (home, *home.parents):
        raise CredentialError(
            f"{path} would be ignored: the home directory and folders above it never assign labels. "
            "Run 'repo bind --shared' in a project folder, or a folder below home such as ~/src/acme."
        )
    merged = read_label_file(path) if path.is_file() else {}
    for key, value in labels.items():
        validate_key(key)
        merged[key] = validate_value(value, key) if value else ""
    content = _FILE_HEADER + "[labels]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in merged.items())
    if len(content.encode()) > _MAX_FILE_BYTES:
        raise CredentialError(f"{path} would exceed {_MAX_FILE_BYTES} bytes.")
    try:
        atomic_write_text(path, content)
    except OSError:
        raise CredentialError(f"Cannot write {path}.") from None
    return path


# Configuration


def label_config(profile: Profile, outputs: Sequence[Output], rules: Rules | None) -> LabelConfig:
    """Combine the central document (its section for this gateway type), the profile and the adapter's outputs."""
    if rules is None:
        return LabelConfig.build(outputs, profile_labels=profile.labels, profile_outputs=profile.outputs)
    return LabelConfig.build(
        outputs, central_labels=rules.labels, central_outputs=rules.outputs.get(profile.gateway_type, {}),
        profile_labels=profile.labels, profile_outputs=profile.outputs, rules=rules, notices=rules.notices,
    )


def load_config(
    store: ProfileStore, profile: Profile, outputs: Sequence[Output], token: Callable[[], str] | None = None,
    *, clock: Callable[[], float] = time.time,
) -> LabelConfig:
    """Load the profile's rules (a published document that does not fit this profile falls back to the cache) and combine."""
    rules = RulesLoader(store, profile, token, clock, validate=lambda loaded: label_config(profile, outputs, loaded))()
    return label_config(profile, outputs, rules)


# Resolution


def resolve_labels(
    store: ProfileStore, profile: Profile, config: LabelConfig, directory: Path, checkout, *,
    explicit: Mapping[str, str | None] | None = None, unknown_explicit: str = "error",
    environ: Mapping[str, str] | None = None, home: Path | None = None,
) -> Attribution:
    """Choose each defined label's value for a directory; the first allowed source with an entry wins.

    1. ``explicit`` (--label; "" or None is "no value"); 2. VKM_LABEL_<KEY> (empty counts as unset);
    3. the personal binding of the origin, then the upstream; 4. the nearest .vkm ("" is "no value");
    5. the central rules (``config.rules``; null is "no value"), each rule against origin then upstream.

    ``checkout`` is the ``repositories.Checkout`` for ``directory`` or None outside Git. Entries for
    unknown keys and from sources a label does not allow are returned as ignored; an explicit unknown
    key raises unless ``unknown_explicit`` is "ignore" (the launch marker). Invalid values, and values
    a strict label does not list, raise CredentialError naming their source. Missing required labels
    are reported in ``missing``, not raised.
    """
    environ = os.environ if environ is None else environ
    identities = checkout.identities if checkout is not None else ()
    known = {definition.key: definition for definition in config.definitions}
    entries: list[tuple[str, str | None, str, str]] = []  # (key, value, source, detail) in source order.
    for key, value in (explicit or {}).items():
        validate_key(key)
        if key not in known and unknown_explicit != "ignore":
            raise CredentialError(
                f"No label '{key}' is defined; defined labels: {', '.join(known) or 'none'}."
            )
        entries.append((key, value if value else None, "explicit", "--label"))
    # Only a defined key's exact variable counts; any other (VKM_LABEL_project too) is unknown under its own name.
    variables = {env_name(key): key for key in known}
    for name, value in environ.items():
        if name.startswith(ENV_PREFIX) and value:
            entries.append((variables.get(name, name), value, "env", name))
    bindings = RepositoryStore(store)
    for identity in identities:
        for key, value in bindings.get(profile.gateway, identity).items():
            entries.append((key, value, "local", f"repo bind for {identity}"))
    path = find_label_file(directory, home)
    if path is not None:
        for key, value in read_label_file(path).items():
            entries.append((key, value or None, "file", str(path)))
    rules = config.rules
    if isinstance(rules, Rules):
        for key, (value, pattern, identity) in rules.match(identities).items():
            entries.append((key, value, "rules", f"rule {pattern} matched {identity}"))

    chosen: dict[str, Label] = {}
    ignored = []
    for key, value, source, detail in entries:
        definition = known.get(key)
        if definition is None or source not in definition.sources:
            shown = value
            if source == "env" and value is not None:
                try:
                    validate_value(value)
                except CredentialError:
                    shown = None  # An unknown variable's value is not checked, so it is not shown either.
            ignored.append(Ignored(key, shown, source, detail, "unknown label" if definition is None else "source not allowed"))
            continue
        if key in chosen:
            continue
        if value is not None:
            try:
                validate_value(value, key)
            except CredentialError as error:
                raise CredentialError(f"{detail}: {error}") from None
            if not definition.accepts(value):
                raise CredentialError(
                    f"{detail}: {json.dumps(value)} is not one of the values of '{key}' "
                    f"(see 'vkm labels values {key}')."
                )
        chosen[key] = Label(key, value, source, detail)
    labels = tuple(chosen.get(key) or Label(key, None) for key in known)
    return Attribution(
        labels, tuple(label.key for label in labels if label.value is None and known[label.key].required),
        tuple(ignored), checkout.origin if checkout is not None else None,
    )


def missing_error(attribution: Attribution, directory: Path, checkout) -> MissingLabelsError:
    where = f"repository {checkout.origin}" if checkout is not None and checkout.origin else f"folder {directory}"
    return MissingLabelsError(attribution.missing, where)


def show_document(config: LabelConfig, attribution: Attribution, directory: Path, checkout) -> dict:
    """``vkm repo show --json``: always this shape; ``headers`` is empty while required labels are missing.

    ``complete`` is also false when the headers cannot be built (e.g. a result over the size limit), so a
    tool that sends ``headers`` only when ``complete`` never sends a request without its attribution.
    """
    message = None
    headers: dict[str, str] = {}
    complete = attribution.complete
    if not complete:
        message = str(missing_error(attribution, directory, checkout))
    else:
        try:
            headers = config.headers(attribution)
        except CredentialError as error:
            complete, message = False, str(error)
        else:
            if not config.definitions:
                message = NO_LABELS_NOTICE
    return _document(config, attribution, complete, headers, message)


def failed_document(config: LabelConfig, error: CredentialError, checkout) -> dict:
    """``vkm repo show --json`` when the labels cannot be resolved (an invalid value or .vkm): incomplete, with the error."""
    attribution = Attribution(repository=checkout.origin if checkout is not None else None)
    return _document(config, attribution, False, {}, str(error))


def _document(config: LabelConfig, attribution: Attribution, complete: bool, headers: dict, message: str | None) -> dict:
    return {
        "labels": config.describe(attribution),
        "missing": list(attribution.missing),
        "ignored": [item.to_dict() for item in attribution.ignored],
        "repository": attribution.repository,
        "complete": complete,
        "headers": headers,
        "message": message,
        "notices": list(config.notices),
    }
